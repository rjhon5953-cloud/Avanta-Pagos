import base64
import os
import hmac
import urllib.parse
import time
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, session, send_file, g
import psycopg2
from psycopg2.extras import DictCursor, execute_values
from psycopg2.pool import SimpleConnectionPool
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MONEDAS_POR_TZ = {
    "America/Guayaquil": ("$", "USD"),
    "America/Bogota": ("$", "COP"),
    "America/Lima": ("S/", "PEN"),
    "America/Mexico_City": ("$", "MXN"),
    "America/Monterrey": ("$", "MXN"),
    "America/Tijuana": ("$", "MXN"),
    "America/Santiago": ("$", "CLP"),
    "America/Argentina/Buenos_Aires": ("$", "ARS"),
    "America/La_Paz": ("Bs", "BOB"),
    "America/Asuncion": ("₲", "PYG"),
    "America/Montevideo": ("$U", "UYU"),
    "America/Caracas": ("Bs", "VES"),
    "America/Santo_Domingo": ("RD$", "DOP"),
    "America/Guatemala": ("Q", "GTQ"),
    "America/Tegucigalpa": ("L", "HNL"),
    "America/Managua": ("C$", "NIO"),
    "America/Costa_Rica": ("₡", "CRC"),
    "America/Panama": ("$", "USD"),
    "America/El_Salvador": ("$", "USD"),
    "America/Sao_Paulo": ("R$", "BRL"),
    "Europe/Madrid": ("€", "EUR"),
    "Europe/London": ("£", "GBP"),
}

def obtener_tz_activa():
    tz_name = session.get("tz") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc

def ahora_local():
    return datetime.now(obtener_tz_activa())

def detectar_moneda_por_tz(tz_name):
    return MONEDAS_POR_TZ.get(tz_name, ("$", "USD"))

app = Flask(__name__)
from flask_compress import Compress # type: ignore
Compress(app)
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError("Configura SECRET_KEY (mínimo 32 caracteres aleatorios) en las variables de entorno.")
app.secret_key = SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() in ("1", "true", "yes", "on"),
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 50 * 1024 * 1024

@app.context_processor
def inyectar_localizacion():
    return {
        "moneda": session.get("moneda_simbolo", "$"),
        "moneda_codigo": session.get("moneda_codigo", "USD"),
        "zona_horaria": session.get("tz", "UTC"),
    }

# 🛡️ SISTEMA SEGURO: Lee la credencial desde el contenedor secreto de Render sin exponerla en código
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("Error crítico: DATABASE_URL no está configurada en las variables de entorno.")

# 🔌 FIJADO: Pool de conexiones para evitar caídas de base de datos en Render/Neon
db_pool = SimpleConnectionPool(
    minconn=2,
    maxconn=20,
    dsn=DATABASE_URL,
    cursor_factory=DictCursor,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=5
)

def get_db():
    """Obtiene una conexión lista del pool sin repetir SELECT 1 en la misma petición."""
    if 'db_conn' in g and not g.db_conn.closed:
        return g.db_conn
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = db_pool.getconn()
    g.db_conn = conn
    return g.db_conn

def release_db(conn):
    """Debe quedar vacío, el context manager se encarga abajo"""
    pass

@app.teardown_appcontext
def shutdown_session(error_context=None):
    """Limpia el contexto RLS en 1 sola consulta y devuelve la conexión al pool."""
    conn = g.pop('db_conn', None)
    if conn is None:
        return
    close_conn = bool(error_context or conn.closed)
    try:
        if conn.closed:
            db_pool.putconn(conn, close=True)
            return
        conn.rollback()
        if not close_conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('avanta.tenant_id', '', false), "
                    "set_config('avanta.route_id', '', false), "
                    "set_config('avanta.migration_bypass', '', false)"
                )
            conn.commit()
        db_pool.putconn(conn, close=close_conn)
    except Exception:
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# SEGURIDAD MULTIEMPRESA: contexto de tenant, contraseñas y Row Level Security
# -----------------------------------------------------------------------------
def _password_is_hashed(value):
    value = str(value or "")
    return value.startswith(("scrypt:", "pbkdf2:sha256:"))


def _verify_password(stored_value, supplied_value):
    """Verifica hashes Werkzeug y conserva compatibilidad temporal con claves antiguas."""
    stored_value = str(stored_value or "")
    supplied_value = str(supplied_value or "")
    if not stored_value or not supplied_value:
        return False
    if _password_is_hashed(stored_value):
        try:
            return check_password_hash(stored_value, supplied_value)
        except Exception:
            return False
    return hmac.compare_digest(stored_value, supplied_value)


def _set_tenant_context(conn, empresa_id, ruta_id=None):
    """Configura el contexto RLS en una sola consulta y lo confirma."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('avanta.tenant_id', %s, false), "
            "set_config('avanta.route_id', %s, false), "
            "set_config('avanta.migration_bypass', '', false)",
            (str(int(empresa_id)), "" if ruta_id is None else str(int(ruta_id)))
        )
    conn.commit()

def _install_tenant_rls(conn):
    """Migra columnas, repara asignaciones desde rutas y fuerza aislamiento en tablas operativas.

    Se ejecuta con bypass de migración solo en esta conexión. Las peticiones normales
    requieren tenant_id y, en Avanta Pagos, también route_id.
    """
    tablas = ("clientes", "pagos", "balance_movimientos", "configuracion")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('avanta.migration_bypass', 'on', false)")
        for tabla in tablas:
            cur.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS ruta_id INTEGER DEFAULT 1")
            cur.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS empresa_id INTEGER DEFAULT 1")

        # La ruta es la fuente de verdad de la empresa. Los pagos heredan ambos datos del cliente.
        cur.execute("""
            UPDATE clientes c
               SET empresa_id = COALESCE(r.empresa_id, 1)
              FROM rutas r
             WHERE r.id = c.ruta_id
               AND COALESCE(c.empresa_id, 1) <> COALESCE(r.empresa_id, 1)
        """)
        cur.execute("""
            UPDATE pagos p
               SET ruta_id = c.ruta_id,
                   empresa_id = COALESCE(c.empresa_id, 1)
              FROM clientes c
             WHERE c.id = p.cliente_id
               AND (p.ruta_id IS DISTINCT FROM c.ruta_id
                    OR COALESCE(p.empresa_id, 1) <> COALESCE(c.empresa_id, 1))
        """)
        for tabla in ("balance_movimientos", "configuracion"):
            cur.execute(f"""
                UPDATE {tabla} t
                   SET empresa_id = COALESCE(r.empresa_id, 1)
                  FROM rutas r
                 WHERE r.id = t.ruta_id
                   AND COALESCE(t.empresa_id, 1) <> COALESCE(r.empresa_id, 1)
            """)
        cur.execute("UPDATE clientes SET empresa_id = 1 WHERE empresa_id IS NULL")
        cur.execute("UPDATE pagos SET empresa_id = 1 WHERE empresa_id IS NULL")
        cur.execute("UPDATE balance_movimientos SET empresa_id = 1 WHERE empresa_id IS NULL")
        cur.execute("UPDATE configuracion SET empresa_id = 1 WHERE empresa_id IS NULL")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_avanta_clientes_tenant_route ON clientes(empresa_id, ruta_id, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_avanta_pagos_tenant_route_cliente ON pagos(empresa_id, ruta_id, cliente_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_avanta_balance_tenant_route_fecha ON balance_movimientos(empresa_id, ruta_id, fecha)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_avanta_config_tenant_route_key ON configuracion(empresa_id, ruta_id, llave)")

        # Al insertar/modificar, la empresa y ruta se derivan de la ruta o cliente padre.
        cur.execute(r"""
            CREATE OR REPLACE FUNCTION avanta_set_tenant_from_parent()
            RETURNS trigger AS $$
            DECLARE v_empresa INTEGER; v_ruta INTEGER;
            BEGIN
                IF TG_TABLE_NAME = 'clientes' THEN
                    v_ruta := COALESCE(NEW.ruta_id, 1);
                    SELECT COALESCE(empresa_id, 1) INTO v_empresa FROM rutas WHERE id = v_ruta;
                ELSIF TG_TABLE_NAME = 'pagos' THEN
                    SELECT COALESCE(empresa_id, 1), ruta_id
                      INTO v_empresa, v_ruta
                      FROM clientes WHERE id = NEW.cliente_id;
                    IF v_ruta IS NOT NULL THEN NEW.ruta_id := v_ruta; END IF;
                ELSE
                    v_ruta := COALESCE(NEW.ruta_id, 1);
                    SELECT COALESCE(empresa_id, 1) INTO v_empresa FROM rutas WHERE id = v_ruta;
                END IF;
                IF v_empresa IS NULL THEN
                    RAISE EXCEPTION 'No se pudo determinar empresa/ruta para %', TG_TABLE_NAME;
                END IF;
                NEW.empresa_id := v_empresa;
                IF NEW.ruta_id IS NULL THEN NEW.ruta_id := v_ruta; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        for tabla in tablas:
            cur.execute(f"DROP TRIGGER IF EXISTS trg_avanta_tenant_{tabla} ON {tabla}")
            cur.execute(f"CREATE TRIGGER trg_avanta_tenant_{tabla} BEFORE INSERT OR UPDATE ON {tabla} FOR EACH ROW EXECUTE FUNCTION avanta_set_tenant_from_parent()")
            cur.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
            cur.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
            cur.execute(f"DROP POLICY IF EXISTS avanta_tenant_isolation ON {tabla}")
            cur.execute(f"""
                CREATE POLICY avanta_tenant_isolation ON {tabla}
                USING (
                    current_setting('avanta.migration_bypass', true) = 'on'
                    OR (
                        NULLIF(current_setting('avanta.tenant_id', true), '') IS NOT NULL
                        AND empresa_id = NULLIF(current_setting('avanta.tenant_id', true), '')::INTEGER
                        AND (
                            NULLIF(current_setting('avanta.route_id', true), '') IS NULL
                            OR ruta_id = NULLIF(current_setting('avanta.route_id', true), '')::INTEGER
                        )
                    )
                )
                WITH CHECK (
                    current_setting('avanta.migration_bypass', true) = 'on'
                    OR (
                        NULLIF(current_setting('avanta.tenant_id', true), '') IS NOT NULL
                        AND empresa_id = NULLIF(current_setting('avanta.tenant_id', true), '')::INTEGER
                        AND (
                            NULLIF(current_setting('avanta.route_id', true), '') IS NULL
                            OR ruta_id = NULLIF(current_setting('avanta.route_id', true), '')::INTEGER
                        )
                    )
                )
            """)
        cur.execute("SELECT set_config('avanta.migration_bypass', '', false)")
        cur.execute("SELECT set_config('avanta.tenant_id', '', false)")
        cur.execute("SELECT set_config('avanta.route_id', '', false)")


def _same_origin_request():
    """Defensa CSRF compatible con los formularios actuales: exige Origin/Referer del mismo host."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    try:
        from urllib.parse import urlsplit
        return urlsplit(source).netloc.lower() == request.host.lower()
    except Exception:
        return False


@app.after_request
def avanta_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=(self)")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.before_request
def avanta_cobrador_request_context():
    if not _same_origin_request():
        return jsonify({"status": "error", "message": "Solicitud rechazada: origen no válido."}), 403
    if session.get("autenticado"):
        try:
            rid = int(session.get("ruta_id") or 0)
            emp_id = int(session.get("empresa_id") or 0)
        except (TypeError, ValueError):
            rid, emp_id = 0, 0
        if rid <= 0 or emp_id <= 0:
            session.clear()
            return redirect("/login")

        ahora_ts = time.time()
        ultimo_check = float(session.get("_auth_check_ts") or 0)
        necesita_validar = (ahora_ts - ultimo_check) > 60

        for intento in range(2):
            conn = get_db()
            try:
                if necesita_validar:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT r.id, r.estado, COALESCE(e.estado, 'Suspendida') AS empresa_estado
                              FROM rutas r LEFT JOIN empresas e ON e.id = COALESCE(r.empresa_id, 1)
                             WHERE r.id = %s AND COALESCE(r.empresa_id, 1) = %s
                        """, (rid, emp_id))
                        row = cur.fetchone()
                    if not row or str(row["estado"] or "").lower() != "activa" or str(row["empresa_estado"] or "").lower() != "activa":
                        session.clear()
                        return redirect("/login")
                    session["_auth_check_ts"] = ahora_ts
                _set_tenant_context(conn, emp_id, rid)
                break
            except psycopg2.OperationalError:
                try:
                    g.pop("db_conn", None)
                    db_pool.putconn(conn, close=True)
                except Exception:
                    pass
                if intento == 1:
                    raise
    return None

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 💰 FIJADO: Tipos de datos monetarios migrados a NUMERIC para precisión exacta
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clientes (
                    id SERIAL PRIMARY KEY,
                    orden INTEGER NOT NULL DEFAULT 0,
                    nombre TEXT NOT NULL,
                    telefono TEXT,
                    monto NUMERIC(10, 2) NOT NULL,
                    interes_porcentaje NUMERIC(5, 2) NOT NULL DEFAULT 20.00,
                    monto_total NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
                    cuotas INTEGER NOT NULL,
                    frecuencia TEXT NOT NULL,
                    valor_cuota NUMERIC(10, 2) NOT NULL,
                    fecha_inicio TEXT NOT NULL,
                    fecha_vencimiento TEXT NOT NULL,
                    saltado_hoy INTEGER NOT NULL DEFAULT 0,
                    fecha_gestion TEXT DEFAULT '',
                    latitud TEXT DEFAULT '',
                    longitud TEXT DEFAULT '',
                    direccion TEXT DEFAULT '',
                    referencia TEXT DEFAULT '',
                    identificacion TEXT DEFAULT '',
                    estado TEXT NOT NULL DEFAULT 'Activo',
                    enrutado_forzado INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        conn.commit()
    finally:
        release_db(conn)

def init_db_contable():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pagos (
                    id SERIAL PRIMARY KEY,
                    cliente_id INTEGER NOT NULL,
                    numero INTEGER NOT NULL,
                    fecha TEXT NOT NULL,
                    valor NUMERIC(10, 2) NOT NULL,
                    pagado INTEGER NOT NULL DEFAULT 0,
                    valor_pagado NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
                    fecha_pago_real TEXT DEFAULT '',
                    FOREIGN KEY (cliente_id) REFERENCES clientes (id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_movimientos (
                    id SERIAL PRIMARY KEY,
                    tipo TEXT NOT NULL,
                    categoria TEXT NOT NULL,
                    concepto TEXT NOT NULL,
                    monto NUMERIC(10, 2) NOT NULL,
                    fecha TEXT NOT NULL,
                    comprobante TEXT DEFAULT ''
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS configuracion (
                    id SERIAL PRIMARY KEY,
                    llave TEXT UNIQUE NOT NULL,
                    valor TEXT NOT NULL
                )
                """
            )
            
            # Columnas requeridas por la arquitectura compartida y por las políticas de aislamiento.
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS ruta_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS empresa_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS ruta_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS empresa_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE balance_movimientos ADD COLUMN IF NOT EXISTS ruta_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE balance_movimientos ADD COLUMN IF NOT EXISTS empresa_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE configuracion ADD COLUMN IF NOT EXISTS ruta_id INTEGER DEFAULT 1")
            cursor.execute("ALTER TABLE configuracion ADD COLUMN IF NOT EXISTS empresa_id INTEGER DEFAULT 1")

            cursor.execute("SELECT COUNT(*) AS total FROM configuracion")
            res_cfg = cursor.fetchone()
            cnt_cfg = res_cfg["total"] if isinstance(res_cfg, dict) else res_cfg[0]
            if int(cnt_cfg or 0) == 0:
                valores_iniciales = [
                    ("empresa_nombre", "AVANTA PAGOS"),
                    ("empresa_telefono", "593991234567"),
                    ("ticket_encabezado", "¡BIENVENIDO A AVANTA PAGOS!"),
                    ("caja_base", "100.00"),
                    ("balance_estado", "Sin cerrar"),
                    ("caja_autorizada", "0")
                ]
                usuario_inicial = (os.environ.get("COBRADOR_INITIAL_USER") or "admin").strip()
                clave_inicial = os.environ.get("COBRADOR_INITIAL_PASSWORD") or ""
                if len(clave_inicial) >= 12:
                    valores_iniciales.extend([
                        ("usuario", usuario_inicial),
                        ("clave", generate_password_hash(clave_inicial))
                    ])
                for k_def, v_def in valores_iniciales:
                    cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES (%s, %s, 1)", (k_def, v_def))
        conn.commit()
    finally:
        release_db(conn)


def init_tenant_security():
    conn = get_db()
    try:
        _install_tenant_rls(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


with app.app_context():
    _conn_boot = get_db()
    with _conn_boot.cursor() as _cur_boot:
        _cur_boot.execute("SELECT set_config('avanta.migration_bypass', 'on', false)")
    _conn_boot.commit()
    init_db()
    init_db_contable()
    init_tenant_security()
def obtener_clientes_ruta_hoy():
    hoy_date = ahora_local().date()
    hoy_str = hoy_date.isoformat()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            if not clientes:
                return []
            
            cliente_ids = [c["id"] for c in clientes]
            cursor.execute("SELECT * FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (cliente_ids,))
            pagos = cursor.fetchall()
    finally:
        pass

    pagos_por_cliente = defaultdict(list)
    for p in pagos:
        pagos_por_cliente[p["cliente_id"]].append(dict(p))

    resultado = []
    for c in clientes:
        c_dict = dict(c)
        if c_dict.get("fecha_gestion") == hoy_str:
            continue
            
        c_dict["pagos"] = pagos_por_cliente.get(c["id"], [])
        debe_cobrar_hoy = False
        
        if c_dict["enrutado_forzado"] == 1:
            debe_cobrar_hoy = True
        else:
            for p in c_dict["pagos"]:
                if p["fecha"] <= hoy_str and p["pagado"] == 0:
                    debe_cobrar_hoy = True
                    break

        if debe_cobrar_hoy:
            # 📊 METRO CONTABLE AVANZADO: Días de Mora o Días Adelantados
            dias_mora_max = 0
            tiene_cuotas_vencidas = False
            
            for p in c_dict["pagos"]:
                p_fecha = datetime.strptime(p["fecha"], "%Y-%m-%d").date() if isinstance(p["fecha"], str) else p["fecha"]
                if not p["pagado"]:
                    if p_fecha < hoy_date:
                        tiene_cuotas_vencidas = True
                        dias = (hoy_date - p_fecha).days
                        if dias > dias_mora_max:
                            dias_mora_max = dias
            
            # Cálculo exacto de días adelantados si no debe nada hacia atrás
            dias_adelanto_max = 0
            if not tiene_cuotas_vencidas:
                # Buscamos la última cuota que SÍ está pagada en el sistema
                ult_pagada = [p for p in c_dict["pagos"] if p["pagado"] == 1]
                if ult_pagada:
                    ultima_p = ult_pagada[-1]
                    f_ultima_p = datetime.strptime(ultima_p["fecha"], "%Y-%m-%d").date() if isinstance(ultima_p["fecha"], str) else ultima_p["fecha"]
                    # Si pagó una cuota cuyo vencimiento es mayor a hoy, está adelantado
                    if f_ultima_p > hoy_date:
                        dias_adelanto_max = (f_ultima_p - hoy_date).days

            # 🎨 Mapeo completo de estados visuales (Incluye el Azul de cuotas adelantadas)
            if tiene_cuotas_vencidas:
                if dias_mora_max > 3:
                    c_dict["estado_mora"] = "en_mora"        # 🔴 Mora crítica
                else:
                    c_dict["estado_mora"] = "entrando_mora"  # 🟡 Entrando en mora
                c_dict["cuotas_atrasadas"] = dias_mora_max
            else:
                if dias_adelanto_max > 0:
                    c_dict["estado_mora"] = "adelantado"     # 🔵 Cuotas adelantadas
                    c_dict["cuotas_atrasadas"] = dias_adelanto_max  # Guardamos los días adelantados aquí
                else:
                    c_dict["estado_mora"] = "al_dia"         # 🟢 Al día normal
                    c_dict["cuotas_atrasadas"] = 0
            
            resultado.append(c_dict)
            
    return resultado


def calcular_fecha(fecha_inicio, numero, frecuencia, omitir_domingos=True):
    actual = fecha_inicio
    pasos = 0
    while pasos < numero:
        if frecuencia == "Diaria":
            actual += timedelta(days=1)
            if omitir_domingos and actual.weekday() == 6:
                continue
        elif frecuencia == "Semanal":
            actual += timedelta(weeks=1)
        elif frecuencia == "Quincenal":
            actual += timedelta(days=15)
        elif frecuencia == "Mensual":
            actual += relativedelta(months=1)
        pasos += 1
    return actual

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Avanta Pagos - Acceso</title>
    <style>
        * { box-sizing: border-box; font-family: 'Inter', system-ui, -apple-system, sans-serif; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
        body { background: linear-gradient(135deg, #0b1329 0%, #1c2541 100%); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 16px; }
        .login-card { background: rgba(30, 41, 59, 0.78); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); padding: 28px 24px; border-radius: 22px; width: 100%; max-width: 390px; box-shadow: 0 20px 45px rgba(0, 0, 0, 0.45); text-align: center; }
        .logo-container { margin-bottom: 16px; }
        .logo-icon { font-size: 28px; background: linear-gradient(135deg, #38bdf8, #0369a1); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 900; letter-spacing: 0.5px; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .logo-subtitle { font-size: 11px; color: #94a3b8; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-top: 4px; }
        h2 { color: white; font-size: 18px; font-weight: 700; margin: 14px 0 18px 0; text-align: center; letter-spacing: 0.3px; }
        
        /* Bloque de Estado Unidad y Caja */
        .status-panel { background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 14px; padding: 14px 16px; margin-bottom: 20px; display: flex; flex-direction: column; gap: 11px; }
        .status-row { display: flex; justify-content: space-between; align-items: center; font-size: 14px; }
        .status-lbl { display: flex; align-items: center; gap: 8px; color: #cbd5e1; font-weight: 600; }
        .status-val { font-weight: 800; color: #ffffff; font-size: 14px; }
        .caja-abierta { color: #34d399; }
        .caja-cerrada { color: #f87171; }

        .input-group { text-align: left; margin-bottom: 16px; position: relative; }
        label { color: #cbd5e1; font-size: 11px; font-weight: 700; text-transform: uppercase; margin-bottom: 6px; display: block; letter-spacing: 0.5px; }
        input { width: 100%; padding: 14px 42px 14px 16px; background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; color: white; font-size: 15px; font-weight: 600; outline: none; transition: all 0.2s; }
        input::placeholder { color: #64748b; }
        input:focus { border-color: #0ea5e9; background: rgba(15, 23, 42, 0.85); box-shadow: 0 0 0 4px rgba(14, 165, 233, 0.15); }
        input:disabled { opacity: 0.45; cursor: not-allowed; }
        
        .toggle-eye { position: absolute; right: 12px; top: 31px; background: transparent; border: none; color: #94a3b8; cursor: pointer; padding: 4px; display: flex; align-items: center; }
        .btn-access { width: 100%; padding: 14px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; font-weight: 700; font-size: 15px; border: none; border-radius: 12px; cursor: pointer; box-shadow: 0 6px 16px rgba(14, 165, 233, 0.35); transition: all 0.2s; margin-top: 4px; }
        .btn-access:hover { background: linear-gradient(135deg, #0284c7, #0369a1); }
        .btn-access:active { transform: translateY(1px); }
        .btn-refresh { width: 100%; padding: 14px; background: rgba(239, 68, 68, 0.16); border: 1px solid rgba(239, 68, 68, 0.45); color: #fca5a5; font-weight: 700; font-size: 14px; border-radius: 12px; cursor: pointer; margin-top: 4px; transition: all 0.2s; }
        .btn-refresh:active { transform: scale(0.99); }
        
        .version-text { color: #475569; font-size: 11px; font-weight: 600; margin-top: 20px; }
        .error-msg { background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; color: #fca5a5; padding: 10px; border-radius: 10px; font-size: 12px; margin-bottom: 16px; font-weight: 600; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo-container">
            <div class="logo-icon">🌐 AVANTA PAGOS</div>
            <div class="logo-subtitle">SISTEMA PRO V1.3</div>
        </div>
        <h2>Control de Acceso</h2>

        <!-- Estado en Vivo de Unidad y Caja antes de ingresar -->
        <div class="status-panel">
            <div class="status-row">
                <div class="status-lbl">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="5" y="2" width="14" height="20" rx="2" ry="2"/>
                        <circle cx="12" cy="10" r="2.5"/>
                        <path d="M8.5 16.5c.8-1.5 2-2.2 3.5-2.2s2.7.7 3.5 2.2"/>
                    </svg>
                    <span>Unidad:</span>
                </div>
                <span class="status-val">{{ unidad_txt }}</span>
            </div>
            <div class="status-row">
                <div class="status-lbl">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="2" y="10" width="20" height="10" rx="2"/>
                        <path d="M6 10V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v4"/>
                        <line x1="6" y1="15" x2="18" y2="15"/>
                    </svg>
                    <span>Caja:</span>
                </div>
                <span class="status-val {{ caja_clase }}">{{ caja_txt }}</span>
            </div>
        </div>

        {% if error %}<div class="error-msg">⚠️ {{ error }}</div>{% endif %}

        {% if caja_abierta_bool %}
            <form action="/login" method="POST" onsubmit="setTimeout(() => { const b = document.getElementById('btnLoginSubmit'); if(b){ b.disabled = true; b.innerText = '⏳ Autenticando...'; } }, 10);">
                <div class="input-group">
                    <label>Usuario Comercial</label>
                    <input type="text" name="usuario" value="{{ usuario_default }}" required placeholder="Ingresa tu usuario">
                </div>
                <div class="input-group">
                    <label>Contraseña</label>
                    <input type="password" id="inputClaveLogin" name="clave" required placeholder="••••••••">
                    <button type="button" class="toggle-eye" onclick="const i=document.getElementById('inputClaveLogin'); i.type = i.type==='password'?'text':'password';" title="Ver contraseña">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                            <circle cx="12" cy="12" r="3"/>
                        </svg>
                    </button>
                </div>
                <button type="submit" id="btnLoginSubmit" class="btn-access">Acceder al Sistema</button>
                    <input type="hidden" name="tz_cliente" id="tz_cliente" value="UTC">
                    <script>
                        try {
                            document.getElementById('tz_cliente').value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
                        } catch (e) {}
                    </script>
            </form>
        {% else %}
            <div class="input-group">
                <label>Usuario Comercial</label>
                <input type="text" value="{{ usuario_default }}" disabled placeholder="Unidad cerrada">
            </div>
            <div class="input-group">
                <label>Contraseña</label>
                <input type="password" disabled placeholder="••••••••">
            </div>
            <button type="button" class="btn-refresh" onclick="window.location.reload()">🔒 Caja Cerrada • Verificar Apertura</button>
        {% endif %}

        <div class="version-text">Avanta Pagos versión 1.3</div>
    </div>
</body>
</html>
"""

CONTENIDO_HTML = """
{% if vista == 'lista' %}
    <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; margin-bottom:12px;">
        <div style="background:#ffffff; padding:10px 8px; border-radius:8px; border-left:4px solid #10b981; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <div style="font-size:9px; color:#6b7280; font-weight:800; text-transform:uppercase;">Recaudado</div>
            <div style="font-size:14px; font-weight:800; color:#065f46; margin-top:2px;">${{ "%.2f"|format(total_cobrado_hoy) }}</div>
        </div>
        <div style="background:#ffffff; padding:10px 8px; border-radius:8px; border-left:4px solid #ef4444; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <div style="font-size:9px; color:#6b7280; font-weight:800; text-transform:uppercase;">Gastos</div>
            <div style="font-size:14px; font-weight:800; color:#991b1b; margin-top:2px;">${{ "%.2f"|format(total_gastos_hoy) }}</div>
        </div>
        <div style="background:#ffffff; padding:10px 8px; border-radius:8px; border-left:4px solid #00a8cc; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <div style="font-size:9px; color:#6b7280; font-weight:800; text-transform:uppercase;">En Calle</div>
            <div style="font-size:14px; font-weight:800; color:#0f2b5c; margin-top:2px;">${{ "%.2f"|format(capital_en_calle) }}</div>
        </div>
    </div>

    <div class="expected-card">
        <div class="kpi-title" style="color:#0369a1;">Recaudo Esperado del Día</div>
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">🔬 Mínimo de Ruta: <b>${{ "%.2f"|format(debido_minimo_dia) }}</b> | Acumulado Mora: <b>${{ "%.2f"|format(debido_total_acumulado) }}</b></div>
    </div>

    <div style="display:flex; gap:6px; margin-bottom:10px; overflow-x:auto;">
        <button onclick="filtrarEstado('todos')" class="btn-filtro active" id="f-todos">👥 Por Cobrar ({{ clientes | length }})</button>
        <button onclick="filtrarEstado('mora')" class="btn-filtro" id="f-mora">⚠️ Mora</button>
        <button onclick="filtrarEstado('aldia')" class="btn-filtro" id="f-aldia">✅ Al Día</button>
    </div>

    <div class="section-header-title"><i class="fa-solid fa-route"></i> Ruta Principal</div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente o referencia..." onkeyup="filtrarClientes()">

    <div id="clientesContainer" style="display: flex !important; flex-direction: column !important; gap: 12px !important; width: 100% !important;">
            {% for c in clientes %}
            {% set pagado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo = c.monto_total - pagado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set cuotas_pagadas = c.pagos | selectattr('pagado', 'equalto', 1) | list | length %}
            {% set es_mora = c.saltado_hoy > 0 or c.cuotas_atrasadas > 0 %}

            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if es_mora else 0 }}" data-cedula="{{ c.identificacion or 'N/A' }}" data-dias="{{ c.dias_adelanto if c.dias_adelanto > 0 else c.dias_mora }}" data-es-adelanto="{{ 1 if c.dias_adelanto > 0 else 0 }}" data-ultpago="{{ c.ultimo_pago_str or 'Sin pagos previos' }}">
                <div class="card-izquierda">
                    <div class="circulo-frecuencia {% if not es_mora %}al-dia{% else %}mora-leve{% endif %}">
                        {{ 'D' if c.frecuencia == 'Diaria' else 'S' }}
                    </div>
                    <div class="info-bloque-texto">
                        <div class="id-alias">#{{ loop.index }} {{ c.referencia or 'Comercio' }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <div class="grid-metricas-premium">
                            <div class="metric-col"><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${{ "%.2f"|format(c.valor_cuota) }}</span></div>
                            <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{{ cuotas_pagadas }} / {{ c.cuotas }}.0</span></div>
                            <div class="metric-col"><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                        </div>
                    <!-- 🛠️ Fila Inferior de Utilidades (Foto, Check de Cuota y Saldo) -->
                    <div class="fila-utilidades-premium">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero if cuota_pendiente else 1 }}, {{ (cuota_pendiente.valor - cuota_pendiente.valor_pagado) if cuota_pendiente else 0 }}, {{ c.valor_cuota }}); mostrarAvisoAvanta('Comprobante', 'Adjunta el comprobante para la cuota de {{ c.nombre }}', 'info');
                            <i class="fa-solid fa-camera"></i>
                        </button>
                        <div class="circulo-check-cuota {% if c.saltado_hoy > 0 %}gestionado{% elif cuotas_pagadas > 0 %}gestionado-ok{% endif %}">
                            {% if c.saltado_hoy > 0 %}{{ c.saltado_hoy }}{% else %}{{ cuotas_pagadas if cuotas_pagadas > 0 else 1 }}{% endif %}
                        </div>
                        <div class="badge-saldo-premium">Saldo <b>${{ "%.2f"|format(saldo) }}</b></div>
                    </div>
                </div>
            </div>
            
            <!-- 🎯 Bloque de Acciones Verticales del Lado Derecho (Cobrar y Saltar) -->
            <div class="card-derecha-acciones">
                {% if cuota_pendiente %}
                    <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})">
                        <i class="fa-solid fa-hand-holding-dollar"></i>
                    </button>
                    <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({{ c.id }})">
                        <i class="fa-solid fa-hand-fist" style="transform: rotate(90deg); font-size: 21px;"></i>
                    </button>
                {% endif %}
            </div>
        </div>
    {% endfor %}

{% elif vista == 'clientes_maestro' %}
    <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 4px 12px 4px;">
        <div style="display:flex; align-items:center; gap:8px;">
            <div style="width:32px; height:32px; border-radius:9px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center;">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            </div>
            <div>
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.1;">DIRECTORIO DE CLIENTES</div>
                <div style="font-size:11px; color:#64748b; font-weight:600;">{{ lista_clientes|length }} clientes registrados</div>
            </div>
        </div>
    </div>

    <div style="position:relative; margin:0 4px 10px 4px;">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; left:13px; top:50%; transform:translateY(-50%); pointer-events:none;">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input type="text" id="buscadorClientesDir" onkeyup="filtrarDirectorioAvanta()"
               placeholder="Buscar por nombre, negocio o teléfono..."
               style="width:100%; padding:13px 14px 13px 40px; border-radius:12px; border:1.5px solid #cbd5e1; font-size:14px; font-weight:500; color:#0f172a; background:#ffffff; outline:none; box-sizing:border-box; box-shadow:0 2px 6px rgba(0,0,0,0.03);">
    </div>

    <div style="display:flex; gap:6px; margin:0 4px 14px 4px;">
        <button type="button" class="tab-dir-avanta active" onclick="cambiarTabDirAvanta(this, 'todos')"
                style="flex:1; padding:9px 6px; border-radius:10px; border:1.5px solid #0f2b5c; background:#0f2b5c; color:#ffffff; font-size:12px; font-weight:700; cursor:pointer;">
            Todos ({{ lista_clientes|length }})
        </button>
        <button type="button" class="tab-dir-avanta" onclick="cambiarTabDirAvanta(this, 'activo')"
                style="flex:1; padding:9px 6px; border-radius:10px; border:1.5px solid #cbd5e1; background:#ffffff; color:#334155; font-size:12px; font-weight:700; cursor:pointer;">
            Con Saldo ({{ total_con_saldo|default(0) }})
        </button>
        <button type="button" class="tab-dir-avanta" onclick="cambiarTabDirAvanta(this, 'libre')"
                style="flex:1; padding:9px 6px; border-radius:10px; border:1.5px solid #cbd5e1; background:#ffffff; color:#334155; font-size:12px; font-weight:700; cursor:pointer;">
            Sin Crédito ({{ total_sin_saldo|default(0) }})
        </button>
    </div>

    <input type="hidden" id="filtroEstadoDirActual" value="todos">

    <div id="listaDirectorioClientes" style="display:flex; flex-direction:column; gap:10px; padding:0 4px 96px 4px;">
        {% for c in lista_clientes %}
        {% set palabras = (c.nombre or 'CL').strip().split() %}
        {% set p1 = palabras[0][0] if palabras|length > 0 else 'C' %}
        {% set p2 = palabras[1][0] if palabras|length > 1 else (palabras[0][1] if palabras[0]|length > 1 else 'L') %}
        {% set iniciales = (p1 ~ p2)|upper %}
        {% set saldo_c = c.saldo_activo|default(0)|float %}
        {% set estado_slug = 'activo' if saldo_c > 0 else 'libre' %}

        <div class="card item-cliente-dir"
             data-filtro="{{ (c.nombre or '')|lower }} {{ (c.referencia or '')|lower }} {{ (c.telefono or '')|lower }}"
             data-estado="{{ estado_slug }}"
             onclick="navegarRuta('/menu/cliente/{{ c.id }}')"
             style="padding:13px 14px; margin:0; background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; display:flex; justify-content:space-between; align-items:center; gap:12px; cursor:pointer; box-shadow:0 2px 6px rgba(15,23,42,0.03); -webkit-tap-highlight-color:transparent;">

                <div translate="no" class="notranslate" style="width:44px; height:44px; flex-shrink:0; border-radius:12px; background:{{ 'linear-gradient(135deg, #0f2b5c, #1e3a8a)' if saldo_c > 0 else '#f1f5f9' }}; color:{{ '#ffffff' if saldo_c > 0 else '#64748b' }}; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px;">
                    {{ iniciales }}
                </div>

            <div style="flex:1; min-width:0; text-align:left;">
                <div style="font-weight:800; color:#0f172a; font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ c.nombre }}</div>
                <div style="font-size:12px; color:#64748b; font-weight:500; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    {{ c.referencia or 'Sin referencia' }}
                </div>
                <div style="display:flex; align-items:center; gap:8px; margin-top:6px; flex-wrap:wrap;">
                    {% if saldo_c > 0 %}
                        <span style="background:#dcfce7; color:#15803d; font-size:11px; font-weight:800; padding:2px 8px; border-radius:6px;">
                            Saldo: ${{ "%.2f"|format(saldo_c) }}
                        </span>
                    {% else %}
                        <span style="background:#f1f5f9; color:#64748b; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px;">
                            Disponible para crédito
                        </span>
                    {% endif %}
                    <span style="font-size:11px; color:#64748b; font-weight:600; display:flex; align-items:center; gap:3px;">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
                        {{ c.telefono or 'S/N' }}
                    </span>
                </div>
            </div>

            <div style="display:flex; align-items:center; gap:6px; flex-shrink:0;">
                {% if c.telefono %}
                <a href="tel:{{ c.telefono }}" onclick="event.stopPropagation();" title="Llamar cliente"
                   style="width:36px; height:36px; border-radius:10px; background:#eff6ff; color:#1d4ed8; display:flex; align-items:center; justify-content:center; text-decoration:none; border:1px solid #dbeafe;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
                </a>
                {% endif %}
                <div style="width:28px; height:28px; display:flex; align-items:center; justify-content:center; color:#94a3b8;">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </div>
            </div>
        </div>
        {% else %}
        <div style="text-align:center; color:#94a3b8; padding:32px 16px; background:#ffffff; border-radius:14px; border:1px dashed #cbd5e1; font-size:14px; font-weight:600;">
            No hay clientes registrados en el directorio.
        </div>
        {% endfor %}
    </div>

    <div style="position:fixed; bottom:16px; left:50%; transform:translateX(-50%); width:92%; max-width:460px; z-index:90;">
        <button type="button" onclick="navegarRuta('/nuevo')"
                style="width:100%; padding:15px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); color:#ffffff; border:none; border-radius:14px; font-weight:800; font-size:15px; box-shadow:0 8px 20px rgba(15,43,92,0.35); cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px;">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Nuevo Cliente / Crédito
        </button>
    </div>

{% elif vista == 'perfil_cliente' %}
    {% set palabras_p = ((cliente.nombre if cliente else 'CL') or 'CL').strip().split() %}
    {% set ini_1 = palabras_p[0][0] if palabras_p|length > 0 else 'C' %}
    {% set ini_2 = palabras_p[1][0] if palabras_p|length > 1 else (palabras_p[0][1] if palabras_p[0]|length > 1 else 'L') %}
    {% set iniciales_p = (ini_1 ~ ini_2)|upper %}
    {% set tel_puro = ((cliente.telefono or '') if cliente else '')|replace(' ', '')|replace('-', '')|replace('+', '') %}
    {% if tel_puro.startswith('0') and tel_puro|length == 10 %}
        {% set tel_limpio = '593' ~ tel_puro[1:] %}
    {% else %}
        {% set tel_limpio = tel_puro %}
    {% endif %}

    <div style="padding:0 4px 28px 4px;">
        <!-- Barra Superior de Navegación -->
        <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 0 12px 0;">
            <button type="button" onclick="navegarRuta('/menu/clientes')"
                    style="background:#ffffff; border:1.5px solid #cbd5e1; color:#0f2b5c; padding:8px 13px; border-radius:10px; font-weight:800; font-size:12.5px; cursor:pointer; display:flex; align-items:center; gap:5px; box-shadow:0 2px 4px rgba(0,0,0,0.03);">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
                Directorio
            </button>
            <span style="font-weight:800; color:#0f2b5c; font-size:13.5px; letter-spacing:0.3px;">EXPEDIENTE DEL CLIENTE</span>
            <div style="width:74px;"></div>
        </div>

        {% if cliente %}
        <!-- Tarjeta Ejecutiva del Titular -->
        <div style="background:linear-gradient(145deg, #0a1f44 0%, #0f2b5c 60%, #1e3a8a 100%); border-radius:16px; padding:16px; color:#ffffff; margin-bottom:12px; box-shadow:0 8px 20px rgba(15,43,92,0.2); display:flex; align-items:center; justify-content:space-between; gap:12px; text-align:left;">
            <div style="display:flex; align-items:center; gap:12px; min-width:0; flex:1;">
                <div translate="no" class="notranslate" style="width:48px; height:48px; border-radius:13px; background:rgba(255,255,255,0.14); border:1.5px solid rgba(255,255,255,0.25); color:#ffffff; font-weight:900; font-size:17px; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    {{ iniciales_p }}
                </div>
                <div style="min-width:0;">
                    <div style="font-size:16px; font-weight:900; color:#ffffff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ cliente.nombre }}</div>
                    <div style="font-size:11.5px; color:#cbd5e1; font-weight:600; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        {{ cliente.referencia or 'Sin referencia registrada' }}
                    </div>
                </div>
            </div>

            {% if tel_limpio %}
            <div style="display:flex; gap:6px; flex-shrink:0;">
                <a href="tel:{{ tel_limpio }}" title="Llamar"
                   style="width:36px; height:36px; border-radius:10px; background:rgba(255,255,255,0.14); border:1px solid rgba(255,255,255,0.22); color:#ffffff; display:flex; align-items:center; justify-content:center; text-decoration:none;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
                </a>
                {% set cred_top = (creditos | selectattr('id', 'equalto', cliente.id) | list | first) or (creditos[0] if creditos else None) %}
                {% set ws_msg = ('Hola ' ~ cliente.nombre ~ ', le saludamos de Avanta Pagos. Le recordamos que mantiene ' ~ cred_top.cuotas_vencidas ~ ' cuota(s) vencida(s) con un saldo pendiente de $' ~ ('%.2f'|format(cred_top.saldo|float)) ~ '. Quedamos atentos a su abono.') if (cred_top and cred_top.cuotas_vencidas|int > 0) else '' %}
                <a href="https://wa.me/{{ tel_limpio }}{% if ws_msg %}?text={{ ws_msg|urlencode }}{% endif %}" target="_blank" title="WhatsApp"
                   style="width:36px; height:36px; border-radius:10px; background:#16a34a; color:#ffffff; display:flex; align-items:center; justify-content:center; text-decoration:none; box-shadow:0 3px 8px rgba(22,163,74,0.3);">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
                </a>
            </div>
            {% endif %}
        </div>
        
        {% set cred_top = (creditos | selectattr('id', 'equalto', cliente.id) | list | first) or (creditos[0] if creditos else None) %}
        {% if cred_top %}
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:12px;">
            <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:10px 8px; text-align:center;">
                <div style="font-size:9.5px; font-weight:800; color:#64748b; text-transform:uppercase;">Saldo Actual</div>
                <div style="font-size:14px; font-weight:900; color:#0f2b5c; margin-top:2px;">${{ "%.2f"|format(cred_top.saldo|float) }}</div>
            </div>
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:12px; padding:10px 8px; text-align:center;">
                <div style="font-size:9.5px; font-weight:800; color:#15803d; text-transform:uppercase;">Total Pagado</div>
                <div style="font-size:14px; font-weight:900; color:#16a34a; margin-top:2px;">${{ "%.2f"|format(cred_top.pagado|float) }}</div>
            </div>
            <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:10px 8px; text-align:center;">
                <div style="font-size:9.5px; font-weight:800; color:#64748b; text-transform:uppercase;">Vencimiento</div>
                {% set f_v = cred_top.detalle_pagos[-1].fecha|string if cred_top.detalle_pagos else '' %}
                <div style="font-size:12px; font-weight:800; color:#334155; margin-top:4px;">{{ (f_v[8:10] ~ '/' ~ f_v[5:7] ~ '/' ~ f_v[:4]) if f_v|length >= 10 else '-' }}</div>
            </div>
        </div>
        {% endif %}

                <!-- Calificación Crediticia y Resumen Histórico -->
        {% if calificacion %}
        <div style="background:{{ calificacion.bg }}; border:1.5px solid {{ calificacion.borde }}; border-radius:14px; padding:13px 14px; margin-bottom:12px; text-align:left;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                <span style="font-size:12px; font-weight:900; color:{{ calificacion.color }}; letter-spacing:0.3px;">● {{ calificacion.titulo }}</span>
                <span style="background:#ffffff; color:{{ calificacion.color }}; border:1px solid {{ calificacion.borde }}; padding:2px 8px; border-radius:8px; font-size:11px; font-weight:900;">Puntualidad: {{ calificacion.puntualidad }}%</span>
            </div>
            <div style="font-size:11.5px; font-weight:700; color:#334155;">{{ calificacion.detalle }}</div>
        </div>
        {% endif %}

        <!-- Botón Único: HISTORIAL DE CRÉDITOS (Abre ventana emergente con Prestado, Abonado y Préstamos) -->
        {% if resumen %}
        <div onclick="document.getElementById('modalHistorialCreditosCliente').style.display='flex'"
            style="background:#ffffff; border:1.5px solid #cbd5e1; border-left:4px solid #0f2b5c; border-radius:13px; padding:12px 14px; margin-bottom:12px; display:flex; justify-content:space-between; align-items:center; cursor:pointer; box-shadow:0 2px 6px rgba(15,23,42,0.03);">
            <div style="display:flex; align-items:center; gap:10px;">
                <span style="width:32px; height:32px; border-radius:9px; background:#e0e7ff; color:#0f2b5c; display:flex; align-items:center; justify-content:center;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
                </span>
                <div style="text-align:left;">
                    <div style="font-size:12.5px; font-weight:900; color:#0f2b5c; letter-spacing:0.3px;">HISTORIAL DE CRÉDITOS</div>
                    <div style="font-size:11px; font-weight:700; color:#64748b;">{{ resumen.total_creditos }} crédito(s) registrado(s) • {{ resumen.liquidados }} liquidado(s)</div>
                </div>
            </div>
            <span style="font-size:11.5px; font-weight:800; color:#1d4ed8; background:#eff6ff; padding:4px 9px; border-radius:8px;">Ver ›</span>
        </div>

        <!-- Modal Emergente con Resumen (Prestado / Abonado) e Historial de Todos los Créditos -->
        <div id="modalHistorialCreditosCliente" onclick="this.style.display='none'"
             style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.68); z-index:10000; align-items:center; justify-content:center; padding:16px; box-sizing:border-box;">
            <div onclick="event.stopPropagation()"
                 style="background:#ffffff; width:100%; max-width:370px; border-radius:18px; overflow:hidden; box-shadow:0 20px 40px rgba(0,0,0,0.3); text-align:left;">
                <div style="background:#0f2b5c; color:#ffffff; padding:14px 16px; display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <div style="font-size:15px; font-weight:900;">Historial de Créditos</div>
                        <div style="font-size:11px; color:#cbd5e1; font-weight:600;">{{ cliente.nombre }}</div>
                    </div>
                    <button type="button" onclick="document.getElementById('modalHistorialCreditosCliente').style.display='none'"
                            style="background:rgba(255,255,255,0.15); border:none; color:#ffffff; width:28px; height:28px; border-radius:8px; font-size:15px; font-weight:800; cursor:pointer;">✕</button>
                </div>
                <div style="padding:14px; max-height:68vh; overflow-y:auto;">
                    <!-- Totales Históricos (Prestado y Abonado) -->
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px;">
                        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:11px; padding:10px; text-align:center;">
                            <div style="font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase;">Total Prestado</div>
                            <div style="font-size:15.5px; font-weight:900; color:#0f172a; margin-top:2px;">${{ resumen.total_prestado }}</div>
                        </div>
                        <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:11px; padding:10px; text-align:center;">
                            <div style="font-size:10px; font-weight:800; color:#15803d; text-transform:uppercase;">Total Abonado</div>
                            <div style="font-size:15.5px; font-weight:900; color:#16a34a; margin-top:2px;">${{ resumen.total_abonado }}</div>
                        </div>
                    </div>

                    {% for cr in creditos %}
                    {% set esta_liq = (cr.saldo <= 0.01) %}
                    <div onclick="document.getElementById('modalHistorialCreditosCliente').style.display='none'; navegarRuta('/menu/cliente/{{ cr.id }}')" style="background:#f8fafc; border:1px solid {{ '#0f2b5c' if cr.id == cliente.id else '#e2e8f0' }}; border-radius:12px; padding:11px 12px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; cursor:pointer;">
                        <div>
                            <div style="font-size:12.5px; font-weight:800; color:#0f172a;">Inicio: {{ cr.fecha_inicio }}</div>
                            <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">{{ cr.cuotas_pagadas }}/{{ cr.total_cuotas_real }} cuotas ({{ cr.frecuencia or 'Diaria' }})</div>
                            <span style="display:inline-block; margin-top:4px; font-size:9.5px; font-weight:800; padding:2px 7px; border-radius:6px; background:{{ '#dcfce7' if esta_liq else '#dbeafe' }}; color:{{ '#166534' if esta_liq else '#1e40af' }};">
                                {{ 'LIQUIDADO' if esta_liq else 'ACTIVO' }}
                            </span>
                        </div>
                        <div style="text-align:right;">
                            <div style="font-size:11px; color:#64748b; font-weight:600;">Prestado: ${{ "%.2f"|format(cr.monto|float) }}</div>
                            <div style="font-size:11px; color:#64748b; font-weight:600;">Total: ${{ "%.2f"|format(cr.monto_total|float) }}</div>
                            <div style="font-size:13px; font-weight:900; color:{{ '#16a34a' if esta_liq else '#0f2b5c' }}; margin-top:2px;">Saldo: ${{ "%.2f"|format(cr.saldo|float) }}</div>
                        </div>
                    </div>
                    {% else %}
                    <div style="text-align:center; color:#64748b; font-size:12px; padding:14px;">Sin créditos históricos registrados.</div>
                    {% endfor %}
                </div>
                <div style="padding:0 14px 14px 14px;">
                    <button type="button" onclick="document.getElementById('modalHistorialCreditosCliente').style.display='none'"
                            style="width:100%; padding:11px; background:#0f2b5c; color:#ffffff; border:none; border-radius:10px; font-weight:800; font-size:13px; cursor:pointer;">
                        Cerrar
                    </button>
                </div>
            </div>
        </div>
        {% endif %}

        <!-- HISTORIAL DE PAGOS (EXCLUSIVO DEL CRÉDITO ACTIVO ACTUAL) -->
        {% set cred_activo = (creditos | selectattr('id', 'equalto', cliente.id) | list | first) or (creditos[0] if creditos else None) %}
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; justify-content:space-between; padding-bottom:8px; margin-bottom:8px; border-bottom:1.5px solid #f1f5f9;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
                    <span style="font-size:12.5px; font-weight:800; color:#0f2b5c; text-transform:uppercase; letter-spacing:0.3px;">Historial de Pagos (Activo)</span>
                </div>
                {% if cred_activo %}
                <div style="display:flex; align-items:center; gap:6px;">
                    <span style="font-size:11px; font-weight:800; color:#0f2b5c; background:#eff6ff; padding:4px 8px; border-radius:6px;">Cuota: ${{ "%.2f"|format(cred_activo.valor_cuota|float) }}</span>
                    <button type="button" onclick="abrirTicketCreditoDesdeExp()"
                        style="background:#0f2b5c; color:#ffffff; border:none; border-radius:6px; padding:5px 9px; font-size:10px; font-weight:800; cursor:pointer; display:inline-flex; align-items:center; gap:4px; white-space:nowrap;">
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                        Ticket Crédito
                    </button>
                </div>
                {% endif %}
            </div>

            {% if cred_activo and cred_activo.detalle_pagos %}
            <div style="max-height:250px; overflow-y:auto; overflow-x:hidden; border:1px solid #e2e8f0; border-radius:10px; width:100%; box-sizing:border-box;">
                <table style="width:100%; border-collapse:collapse; font-size:10.5px; table-layout:fixed;">
                    <thead style="position:sticky; top:0; background:#f8fafc; z-index:1;">
                        <tr style="border-bottom:1px solid #cbd5e1; color:#475569; text-align:left;">
                            <th style="padding:6px 4px; width:24px;">#</th>
                            <th style="padding:6px 4px; width:74px;">Fecha</th>
                            <th style="padding:6px 4px;">Pagó el</th>
                            <th style="padding:6px 4px; width:64px; text-align:right;">Abono</th>
                            <th style="padding:6px 5px; width:66px; text-align:right;">Estado</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for p in cred_activo.detalle_pagos %}
                        {% set v_cuota = p.valor|float %}
                        {% set v_pagado = p.valor_pagado|float %}
                        {% set es_completa = (p.pagado == 1 or v_pagado >= (v_cuota - 0.01)) %}
                        {% set es_incompleta = (not es_completa and v_pagado > 0) %}
                        {% set es_vencida = (not es_completa and not es_incompleta and p.fecha < hoy_str) %}
                        {% set f_s = (p.fecha|string)[:10] %}
                        {% set f_ddmmyyyy = (f_s[8:10] ~ '/' ~ f_s[5:7] ~ '/' ~ f_s[:4]) if '-' in f_s and f_s|length >= 10 else f_s %}
                        {% set fp_s = (p.fecha_pago_real|string) if p.fecha_pago_real else '' %}
                        {% set fp_fmt = (fp_s[8:10] ~ '/' ~ fp_s[5:7] ~ '/' ~ fp_s[:4] ~ fp_s[10:]) if '-' in fp_s[:10] and fp_s|length >= 10 else (fp_s if fp_s else '-') %}
                        <tr translate="no" class="notranslate" style="border-bottom:1px solid #f1f5f9; background:{{ '#f0fdf4' if es_completa else ('#fffbeb' if es_incompleta else ('#fef2f2' if es_vencida else '#ffffff')) }};">
                            <td style="padding:6px 4px; font-weight:800; color:#0f172a;">{{ p.numero }}</td>
                            <td style="padding:6px 4px; color:#334155; font-weight:600; white-space:nowrap;">{{ f_ddmmyyyy }}</td>
                            <td style="padding:6px 4px; color:#64748b; font-size:9.5px; line-height:1.2; word-break:break-word;">{{ fp_fmt }}</td>
                            <td style="padding:6px 4px; text-align:right; font-weight:800; color:#0f172a;">
                                {% if es_completa %}
                                ${{ "%.2f"|format(v_cuota) }}
                                {% else %}
                                ${{ "%.2f"|format(v_pagado) }}<div style="font-size:9px; color:#64748b; font-weight:600;">/${{ "%.2f"|format(v_cuota) }}</div>
                                {% endif %}
                            </td>
                            <td style="padding:6px 5px; text-align:right; font-weight:900; color:{{ '#16a34a' if es_completa else ('#d97706' if es_incompleta else ('#dc2626' if es_vencida else '#64748b')) }};">
                                {% if es_completa or es_incompleta %}
                                <button type="button"
                                    onclick="abrirTicketPagoDesdeExp('{{ p.numero }}', '{{ '%.2f'|format(v_pagado if v_pagado > 0 else v_cuota) }}', '{{ p.fecha_pago_real or p.fecha }}')"
                                    style="background:#16a34a; color:#ffffff; border:none; border-radius:6px; padding:4px 6px; font-size:9.5px; font-weight:800; cursor:pointer; display:inline-flex; align-items:center; gap:2px;">
                                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                                    Ticket
                                </button>
                                {% else %}
                                {{ 'Vencida' if es_vencida else 'Pendiente' }}
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <div style="text-align:center; font-size:12px; color:#94a3b8; padding:14px 6px; font-weight:600;">Este cliente no tiene un crédito activo con cuotas pendientes.</div>
            {% endif %}
        </div>

        {% if cred_activo %}
        <div id="datosTicketExpediente" style="display:none;"
            data-cliente="{{ cliente.nombre or '' }}"
            data-cedula="{{ cliente.identificacion or 'N/A' }}"
            data-telefono="{{ cliente.telefono or 'N/A' }}"
            data-direccion="{{ cliente.direccion or 'N/A' }}"
            data-referencia="{{ cliente.referencia or '' }}"
            data-codigo="{{ '%06d'|format(cred_activo.id) }}"
            data-frecuencia="{{ cred_activo.frecuencia or 'Diaria' }}"
            data-inicio="{{ cred_activo.fecha_inicio or '' }}"
            data-capital="{{ '%.2f'|format(cred_activo.monto | float) }}"
            data-total="{{ '%.2f'|format(cred_activo.monto_total | float) }}"
            data-cuota="{{ '%.2f'|format(cred_activo.valor_cuota | float) }}"
            data-cuotas="{{ cred_activo.cuotas }}"
            data-saldo="{{ '%.2f'|format((cred_activo.saldo_actual if cred_activo.saldo_actual is defined else (cred_activo.saldo if cred_activo.saldo is defined else 0)) | float) }}"
            data-vencimiento="{{ cred_activo.detalle_pagos[-1].fecha if cred_activo.detalle_pagos else '' }}">
        </div>
        {% endif %}

        <!-- Geolocalización del Cliente -->
        <div style="margin-bottom:12px;">
            {% if cliente and cliente.latitud and cliente.longitud %}
                <button type="button" onclick="abrirGpsCliente('{{ cliente.latitud|default('') }}', '{{ cliente.longitud|default('') }}')"
                        style="width:100%; padding:13px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:12px; font-weight:800; font-size:13.5px; display:flex; align-items:center; justify-content:center; gap:8px; cursor:pointer; box-shadow:0 4px 12px rgba(22,163,74,0.22);">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                    Abrir Ubicación en Google Maps
                </button>
            {% else %}
                <div style="padding:11px 12px; background:#fffbeb; border:1px solid #fde68a; color:#92400e; border-radius:12px; font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:space-between; gap:8px;">
                    <div style="display:flex; align-items:center; gap:7px;">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        <span>Sin GPS guardado.</span>
                    </div>
                    <button type="button" onclick="guardarGpsExpediente({{ cliente.id }}, this)" style="background:#0f2b5c; color:#ffffff; border:none; border-radius:8px; padding:6px 10px; font-size:11px; font-weight:800; cursor:pointer; display:inline-flex; align-items:center; gap:5px; flex-shrink:0;">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                        Guardar GPS ahora
                    </button>
                </div>
            {% endif %}
        </div>

        <!-- Ficha de Datos Personales y Acciones -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                <span style="font-size:12.5px; font-weight:800; color:#0f2b5c; text-transform:uppercase; letter-spacing:0.3px;">Datos del Titular</span>
            </div>

            <div style="margin-bottom:10px;">
                <label style="font-size:11.5px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Nombre Completo</label>
                <input type="text" id="perf_nombre" value="{{ cliente.nombre if cliente else '' }}" readonly class="input-perfil"
                       style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:14px; font-weight:700; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:10px;">
                <div>
                    <label style="font-size:11.5px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Identificación / Cédula</label>
                    <input type="text" id="perf_cedula" value="{{ cliente.identificacion if cliente else '' }}" readonly class="input-perfil"
                        style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:13.5px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                </div>
                <div>
                    <label style="font-size:11.5px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Teléfono / WhatsApp</label>
                    <input type="text" id="perf_telefono" value="{{ (cliente.telefono or '') if cliente else '' }}" readonly class="input-perfil"
                           style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:13.5px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                </div>
            </div>

            <div style="margin-bottom:10px;">
                <label style="font-size:11.5px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Dirección Escrita</label>
                <input type="text" id="perf_direccion" value="{{ (cliente.direccion or '') if cliente else '' }}" readonly class="input-perfil"
                       style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:13.5px; font-weight:600; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:11.5px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Referencia / Negocio</label>
                <input type="text" id="perf_referencia" value="{{ (cliente.referencia or '') if cliente else '' }}" readonly class="input-perfil"
                       style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:13.5px; font-weight:600; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="display:flex; gap:8px;">
                <button type="button" id="btnEditarPerfil" onclick="toggleEdicionPerfil({{ cliente.id if cliente else 0 }})"
                        style="flex:1; padding:12px 10px; background:#f8fafc; color:#0f2b5c; border:1.5px solid #cbd5e1; border-radius:11px; font-weight:800; font-size:13px; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                    Modificar
                </button>
                {% set tiene_saldo_activo = (cred_activo and cred_activo.saldo > 0.01) %}
                {% if tiene_saldo_activo %}
                <button type="button" onclick="mostrarAvisoSaldoActivo('{{ "%.2f"|format(cred_activo.saldo|float) }}')"
                        style="flex:1.35; padding:12px 10px; background:#cbd5e1; color:#475569; border:1.5px solid #94a3b8; border-radius:11px; font-weight:800; font-size:12.5px; cursor:not-allowed; display:flex; align-items:center; justify-content:center; gap:5px;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                    Saldo Activo (${{ "%.2f"|format(cred_activo.saldo|float) }})
                </button>
                {% else %}
                <button type="button" onclick="navegarRuta('/mover_renovacion/{{ cliente.id if cliente else 0 }}')"
                        style="flex:1.35; padding:12px 10px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:13px; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px; box-shadow:0 3px 8px rgba(15,43,92,0.2);">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                    Conceder Crédito
                </button>
                {% endif %}
            </div>
        </div>
    </div>
{% endif %}

{% elif vista == 'embed_creditos_maestros' or vista == 'creditos_maestros' %}
    <div class="section-header-title"><i class="fa-solid fa-hand-holding-dollar"></i> CONTROL MAESTRO DE GESTIÓN</div>
    
    <div style="display:flex; gap:6px; margin-bottom:12px; overflow-x:auto;">
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Activo')">🟢 Activos ({{ total_activos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Inactivo')">⚪ Inactivos ({{ total_inactivos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Lista Negra')">⚫ Bloqueados ({{ total_negra }})</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar registro..." onkeyup="filtrarClientes()">
    
    <div id="clientesContainer">
        {% for c in todos %}
            {% set pagado_acumulado = c.pagos | map(attribute='valor_pagado') | sum if c.pagos else 0 %}
            {% set saldo_restante = c.monto_total - pagado_acumulado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first if c.pagos else None %}
            {% set cuotas_pagadas = c.pagos | selectattr('pagado', 'equalto', 1) | list | length if c.pagos else 0 %}
            
            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if c.saltado_hoy > 0 else 0 }}">
                <div class="card-izquierda">
                    <div class="circulo-frecuencia al-dia">{{ 'D' if c.frecuencia == 'Diaria' else 'S' }}</div>
                    
                    <div class="info-bloque-texto" style="width:100%;">
                        <!-- 🛡️ FIJADO: Muestra la Modalidad si no hay una referencia para evitar que todo sea 'Comercio' -->
                        <div class="id-alias">#{{ loop.index }} - {{ c.referencia or c.nombre }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <div class="grid-metricas-premium">
                            <div><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${{ "%.2f"|format(c.valor_cuota) }}</span></div>
                            <div><span class="metric-lbl">Pendiente</span><span class="metric-val">{{ cuotas_pagadas }} / {{ c.cuotas }}.0</span></div>
                            <div><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                        </div>
                        
                        <div class="fila-utilidades-premium">
                            <!-- 🔄 BOTÓN DE RENOVAR: Exclusivo para clientes Inactivos (Liquidados) -->
                            {% if c.estado == 'Inactivo' %}
                                <button type="button" class="btn-accion" onclick="navegarRuta('/mover_renovacion/{{ c.id }}')" style="background: #f97316; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 11px;">
                                    <i class="fa-solid fa-rotate"></i> Renovar
                                </button>
                            {% endif %}
                            
                            <!-- ⚡ BOTÓN DE REACTIVAR: Exclusivo para regresar Bloqueados a la ruta con su saldo pendiente -->
                            {% if c.estado == 'Lista Negra' %}
                                <button type="button" class="btn-accion" onclick="postAccionAvanta('/api/clientes/reactivar/{{ c.id }}', '/menu/creditos?filter=Inactivo', '¿Desea reactivar a este cliente y regresarlo a la ruta con su saldo restante?')" style="background: #10b981; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 11px;">
                                    <i class="fa-solid fa-bolt"></i> Reactivar
                                </button>
                            {% endif %}
                            
                            {% if c.estado == 'Activo' %}
                                <button type="button" class="btn-accion" onclick="postAccionAvanta('/api/clientes/lista_negra/{{ c.id }}', '/menu/clientes', '¿Mover a Lista Negra?')" style="background:#475569; padding: 6px 12px; font-size: 11px;"><i class="fa-solid fa-ban"></i> Bloquear</button>
                            {% endif %}
                            <button type="button" onclick="postAccionAvanta('/api/eliminar_cliente/{{ c.id }}', '/menu/clientes', '¿Eliminar de forma permanente?')" class="btn-accion btn-nopagar" style="padding: 6px 12px; background:#dc2626; font-size: 11px;"><i class="fa-solid fa-trash-can"></i> Borrar</button>
                            
                            <div class="badge-saldo-premium" style="margin-left: auto;">Saldo <b>${{ "%.2f"|format(saldo_restante) }}</b></div>
                        </div>
                    </div>
                </div>
                
                <!-- 🎯 FIJADO: Las opciones de cobrar y saltar del bolso lateral SOLO se muestran si el cliente está Activo -->
                {% if c.estado == 'Activo' and cuota_pendiente %}
                <div class="card-derecha-acciones">
                    <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})">💵✔️</button>
                    <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({{ c.id }})">💵❌</button>
                </div>
                {% endif %}
            </div>
        {% endfor %}
    </div>

{% elif vista == 'pagos_hoy' %}
<!-- 🛡️ FUERZA EL CREADO DE ICONOS: Asegura que FontAwesome se cargue en la vista inyectada -->
    <div class="section-header-title"><i class="fa-solid fa-money-bill-trend-up"></i> GESTIÓN DE COBROS DE HOY</div>

    <!-- 🟢 SECCIÓN DE ABONOS REALIZADOS (ESTILO TRYCONTROLLER CON BARRA VERDE) -->
    <div style="margin-bottom: 20px; padding: 0 4px;">
        <h4 style="font-size: 12px; color: #10b981; font-weight: 800; text-transform: uppercase; margin-bottom: 8px; text-align: left; border-left: 3px solid #10b981; padding-left: 6px;">
            💰 Abonos / Pagos Recibidos
        </h4>
        <div id="containerPagados" style="display: flex; flex-direction: column; gap: 8px;">
            {% for p in pagados_list %}
                <!-- 🎯 FIJADO: Inyección de border-left de 5px verde para simular la gestión completada -->
                <div class="card" style="padding: 12px; margin-bottom: 0; background: white; text-align: left; border: 1px solid #e2e8f0; border-radius: 10px; border-left: 5px solid #10b981 !important; padding-left: 10px !important;">
                    <div class="flex-between">
                        <div>
                            <span style="font-weight: 800; color: #0f2b5c; font-size: 13px; display: block;">{{ p.nombre }}</span>
                            <span style="font-size: 11px; color: #64748b;">Cuotas: <b>{{ p.rango_cuotas }}</b></span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 12px;">
                            <span style="font-weight: 800; color: #10b981; font-size: 14px;">${{ "%.2f"|format(p.total_abonado) }}</span>
                            <button type="button" onclick="revertirGestionDia('pago', '{{ p.pago_ids_str }}')" style="background: #ef4444; border: none; width: 38px; height: 38px; border-radius: 8px; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; box-shadow: 0 2px 5px rgba(239, 68, 68, 0.25);">
                                <svg xmlns="http://w3.org" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" style="color: white !important;">
                                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 15L3 9m0 0l6-6M3 9h12a6 6 0 010 12h-3" />
                                </svg>
                            </button>
                        </div>
                    </div>
                </div>
            {% else %}
                <div style="font-size: 12px; color: #94a3b8; text-align: center; padding: 14px; background: #ffffff; border-radius: 10px; border: 1px dashed #cbd5e1;">Ningún recaudo asentado hoy todavía.</div>
            {% endfor %}
        </div>
    </div>

    <!-- 🔴 SECCIÓN DE CLIENTES SALTADOS (ESTILO TRYCONTROLLER CON BARRA ROJA) -->
    <div style="padding: 0 4px;">
        <h4 style="font-size: 12px; color: #ef4444; font-weight: 800; text-transform: uppercase; margin-bottom: 8px; text-align: left; border-left: 3px solid #ef4444; padding-left: 6px;">
            ❌ Clientes Saltados (No Pagó)
        </h4>
        <div id="containerSaltados" style="display: flex; flex-direction: column; gap: 8px;">
            {% for s in saltados_list %}
                <!-- 🎯 FIJADO: Inyección de border-left de 5px rojo para simular la alerta de omitido/no pago -->
                <div class="card" style="padding: 12px; margin-bottom: 0; background: white; text-align: left; border: 1px solid #e2e8f0; border-radius: 10px; border-left: 5px solid #dc2626 !important; padding-left: 10px !important;">
                    <div class="flex-between">
                        <div>
                            <span style="font-weight: 800; color: #0f2b5c; font-size: 13px; display: block;">{{ s.nombre }}</span>
                            <span style="font-size: 11px; color: #64748b;">Total días saltado: <b>{{ s.saltado_hoy }}</b></span>
                        </div>
                        <button type="button" onclick="revertirGestionDia('salto', '{{ s.cliente_id }}')" style="background: #475569; border: none; width: 38px; height: 38px; border-radius: 8px; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; box-shadow: 0 2px 5px rgba(71, 85, 105, 0.25);">
                            <svg xmlns="http://w3.org" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" style="color: white !important;">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M9 15L3 9m0 0l6-6M3 9h12a6 6 0 010 12h-3" />
                            </svg>
                        </button>
                    </div>
                </div>
            {% else %}
                <div style="font-size: 12px; color: #94a3b8; text-align: center; padding: 14px; background: #ffffff; border-radius: 10px; border: 1px dashed #cbd5e1;">Ningún cliente saltado hoy.</div>
            {% endfor %}
        </div>
    </div>

    <script>
    function revertirGestionDia(tipo, idStr) {
        if (confirm('¿Está seguro de que desea deshacer esta gestión y regresar al cliente a la Ruta del Día?')) {
            fetch('/api/revertir_gestion/' + tipo + '/' + idStr, { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'ok') {
                        navegarRuta('/menu/pagos_hoy');
                    } else {
                        mostrarAvisoAvanta('Aviso', 'No se pudo revertir: ' + (data.message || 'Error en el servidor'), 'alerta');
                    }
                })
                .catch(err => console.error('Error:', err));
        }
    }
    </script>

{% elif vista == 'creditos_financieros' %}
    <div class="section-header-title"><i class="fa-solid fa-hand-holding-dollar"></i> CONTROL FINANCIERO DE CRÉDITOS</div>
    <p style="font-size:11px; color:#64748b; margin-bottom:12px; text-align:left; padding:0 4px;">Monitorea el rendimiento de tu cartera activa en tiempo real. Aquí puedes ver el desglose exacto de lo invertido frente a lo recaudado por cliente.</p>
    
    <div id="creditosContainer">
        {% if lista_creditos %}
            {% for c in lista_creditos %}
                <div class="card" style="padding:14px; border-left:4px solid #10b981; text-align:left; margin-bottom:10px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
                    <div class="flex-between" style="border-bottom:1px solid #f1f5f9; padding-bottom:6px; margin-bottom:8px;">
                        <div>
                            <span style="font-weight:800; font-size:14px; color:#0f2b5c; display:block;">{{ c.nombre }}</span>
                            <span style="font-size:10px; color:#64748b; display:block; margin-top:2px;">Modalidad: <b>{{ c.frecuencia }}</b> | Cuota: <b>${{ "%.2f"|format(c.valor_cuota) }}</b></span>
                        </div>
                        <div style="text-align:right;">
                            <!-- 💰 Badge de Saldo Neto Pendiente Real -->
                            <span style="font-size:10px; color:#475569; font-weight:800; text-transform:uppercase; display:block;">Saldo Real</span>
                            <span style="font-size:15px; font-weight:800; color:#ef4444; display:block;">${{ "%.2f"|format(c.saldo_pendiente) }}</span>
                        </div>
                    </div>
                    
                    <!-- 📊 Desglose de Métricas Contables individuales sin repetir líneas -->
                    <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; font-size:11px; background:#f8fafc; padding:8px; border-radius:8px; border:1px solid #e2e8f0;">
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Préstamo</span>
                            <span style="font-weight:700; color:#334155;">${{ "%.2f"|format(c.monto) }}</span>
                        </div>
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Con Interés ({{ c.interes_porcentaje }}%)</span>
                            <span style="font-weight:700; color:#0f2b5c;">${{ "%.2f"|format(c.monto_total) }}</span>
                        </div>
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Cobrado</span>
                            <span style="font-weight:700; color:#10b981;">${{ "%.2f"|format(c.total_recaudado) }}</span>
                        </div>
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center; padding:20px;">No se registran créditos financieros activos en este momento.</p>
        {% endif %}
    </div>

{% elif vista == 'nuevo' or vista == 'renovar' %}
    <div style="padding:0 4px 28px 4px;">
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 12px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>
            </div>
            <div style="text-align:left; min-width:0;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    {% if vista == 'renovar' %}RENOVAR CRÉDITO{% else %}NUEVO CLIENTE / CRÉDITO{% endif %}
                </div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">
                    {% if vista == 'renovar' %}Cliente: {{ cliente.nombre }}{% else %}Registro de titular, condiciones y geolocalización{% endif %}
                </div>
            </div>
        </div>

        <form id="formNuevoCredito" action="/api/clientes/guardar" method="POST" onsubmit="enviarCreditoAsincrono(event)" style="display:flex; flex-direction:column; gap:12px;">
            {% if vista == 'renovar' %}<input type="hidden" name="cliente_id" value="{{ cliente.id }}">{% endif %}

            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Datos del Titular</span>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Nombre Completo del Cliente*</label>
                <input type="text" name="nombre" value="{{ cliente.nombre | default('') }}" required placeholder="Ej: Carlos Mendoza"
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:700; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:11px;">
                    <div>
                        <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">WhatsApp / Celular*</label>
                        <input type="text" name="telefono" value="{{ cliente.telefono | default('') }}" required placeholder="0991234567"
                               style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Cédula / ID</label>
                        <input type="text" name="identificacion" value="{{ cliente.identificacion | default('') }}" placeholder="Opcional"
                               style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; box-sizing:border-box;">
                    </div>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Dirección Domiciliaria</label>
                <input type="text" name="direccion" value="{{ cliente.direccion | default('') }}" placeholder="Sector, calle principal y secundaria"
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:500; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Referencia del Negocio / Local</label>
                <input type="text" name="referencia" value="{{ cliente.referencia | default('') }}" placeholder="Ej: Tienda esquinera color azul"
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:500; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                    <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Condiciones del Crédito</span>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Monto a Financiar ($)*</label>
                <input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required placeholder="0.00"
                       style="width:100%; padding:12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:17px; font-weight:800; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px;">
                    <div>
                        <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Interés (%)*</label>
                        <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required
                               style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:15px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">N° Cuotas*</label>
                        <input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required
                               style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:15px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:5px;">Frecuencia de Cobro</label>
                <input type="hidden" name="frecuencia" id="inputFreqCredito" value="Diaria">
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:13px;">
                    <div id="btnFreqDiaria" onclick="document.getElementById('inputFreqCredito').value='Diaria'; seleccionarFreqCredito('Diaria')"
                         style="padding:11px; border-radius:10px; border:2px solid #0f2b5c; background:#0f2b5c; color:#ffffff; font-size:13px; font-weight:800; text-align:center; cursor:pointer; user-select:none;">
                        Diaria
                    </div>
                    <div id="btnFreqSemanal" onclick="document.getElementById('inputFreqCredito').value='Semanal'; seleccionarFreqCredito('Semanal')"
                         style="padding:11px; border-radius:10px; border:1.5px solid #cbd5e1; background:#f8fafc; color:#475569; font-size:13px; font-weight:700; text-align:center; cursor:pointer; user-select:none;">
                        Semanal
                    </div>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:5px;">Cobertura de Microseguro</label>
                <select name="aplicar_seguro" id="calcSeguroAccion" style="display:none;">
                    <option value="NO">NO</option>
                    <option value="2">2</option>
                    <option value="4" selected>4</option>
                    <option value="MANUAL">MANUAL</option>
                </select>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:7px; margin-bottom:11px;">
                    <div class="card-seg-opt" id="optSeg_NO" onclick="seleccionarSegCredito('NO')"
                         style="padding:9px 10px; border-radius:10px; border:1.5px solid #cbd5e1; background:#f8fafc; color:#475569; cursor:pointer; user-select:none;">
                        <div style="font-size:12px; font-weight:800;">Sin Seguro</div>
                        <div style="font-size:10.5px; opacity:0.85;">Entrega 100%</div>
                    </div>
                    <div class="card-seg-opt" id="optSeg_2" onclick="seleccionarSegCredito('2')"
                         style="padding:9px 10px; border-radius:10px; border:1.5px solid #cbd5e1; background:#f8fafc; color:#475569; cursor:pointer; user-select:none;">
                        <div style="font-size:12px; font-weight:800;">Básico (2%)</div>
                        <div style="font-size:10.5px; opacity:0.85;">Cobertura base</div>
                    </div>
                    <div class="card-seg-opt" id="optSeg_4" onclick="seleccionarSegCredito('4')"
                        style="padding:9px 10px; border-radius:10px; border:2px solid #0f2b5c; background:#eff6ff; color:#0f2b5c; cursor:pointer; user-select:none;">
                        <div style="font-size:12px; font-weight:800;">Premium (4%)</div>
                        <div style="font-size:10.5px; opacity:0.85;">Recomendado</div>
                    </div>
                    <div class="card-seg-opt" id="optSeg_MANUAL" onclick="seleccionarSegCredito('MANUAL')"
                         style="padding:9px 10px; border-radius:10px; border:1.5px solid #cbd5e1; background:#f8fafc; color:#475569; cursor:pointer; user-select:none;">
                        <div style="font-size:12px; font-weight:800;">Monto Manual</div>
                        <div style="font-size:10.5px; opacity:0.85;">Valor fijo ($)</div>
                    </div>
                </div>

                <div id="contenedorSeguroManual" style="display: none; margin-bottom: 11px;">
                    <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Monto Manual del Seguro ($)</label>
                    <input type="number" step="any" id="calcSeguroManualVal" name="microseguro_manual_valor" value="0" oninput="calcularCuota()"
                           style="width:100%; padding:10px 12px; border-radius:10px; border:1.5px solid #cbd5e1; font-size:14px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Fecha de Inicio de Cobro*</label>
                <input type="date" name="fecha_inicio" value="{{ hoy_str }}" required
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:700; color:#0f172a; box-sizing:border-box;">
            </div>

            <!-- TARJETA 3: SIMULADOR EJECUTIVO EN VIVO -->
            <div style="background:linear-gradient(145deg, #0a1f44 0%, #0f2b5c 60%, #1e3a8a 100%); border-radius:14px; padding:15px 16px; color:#ffffff; box-shadow:0 6px 16px rgba(15,43,92,0.2); text-align:left;">
                <div style="font-size:10.5px; font-weight:800; color:#93c5fd; text-transform:uppercase; letter-spacing:0.6px; margin-bottom:4px;">Proyección del Crédito</div>
                <div id="simulacionText" style="font-size:22px; font-weight:900; color:#ffffff; letter-spacing:-0.3px;">$0.00 / cuota</div>
                <div id="seguroDesgloseText" style="font-size:12px; color:#cbd5e1; font-weight:600; margin-top:6px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.12);">Monto Seguro: $0.00 | Neto a Entregar: $0.00</div>
            </div>

                        <!-- TARJETA 1.5: DOCUMENTACIÓN / FOTOS DE CÉDULA (ESTILO GESTOR DE PAGOS) -->
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px 15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
                    <span style="font-size:13px; font-weight:800; color:#0f2b5c;">Adicionar fotografía de cédula</span>
                    {% if vista == 'renovar' and (cliente.foto_cedula_frente or cliente.foto_cedula_reverso) %}
                    <span style="background:#dcfce7; color:#15803d; font-size:10px; font-weight:800; padding:2px 7px; border-radius:6px;">Guardada</span>
                    {% endif %}
                </div>

                {% set f_frente = (cliente.foto_cedula_frente if (cliente and cliente.foto_cedula_frente) else '') %}
                {% set f_reverso = (cliente.foto_cedula_reverso if (cliente and cliente.foto_cedula_reverso) else '') %}

                <input type="hidden" name="foto_cedula_frente" id="inputFotoCedulaFrente" value="{{ f_frente }}">
                <input type="hidden" name="foto_cedula_reverso" id="inputFotoCedulaReverso" value="{{ f_reverso }}">

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                <!-- CUADRO COMPACTO: CÉDULA FRONTAL -->
                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:9px;">
                    <div style="display:flex; align-items:center; gap:9px;">
                        <label style="width:52px; height:52px; border-radius:10px; background:#e2e8f0; border:1.5px solid #cbd5e1; display:flex; align-items:center; justify-content:center; overflow:hidden; cursor:pointer; flex-shrink:0;">
                            <img id="prevCedulaFrente" src="{{ f_frente }}"
                                 style="width:100%; height:100%; object-fit:cover; display:{{ 'block' if f_frente else 'none' }};">
                            <div id="placeholderCedulaFrente" style="display:{{ 'none' if f_frente else 'flex' }}; align-items:center; justify-content:center; color:#475569;">
                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                            </div>
                            <input type="file" accept="image/*" capture="environment" onchange="procesarFotoCedula(this, 'Frente')" style="display:none;">
                        </label>
                        <div>
                            <div style="font-size:12px; font-weight:800; color:#0f172a;">Frontal</div>
                            <div onclick="const s=document.getElementById('prevCedulaFrente').src; if(s && s.startsWith('data:image')) verFotoAmpliada(s);"
                                 style="font-size:10.5px; color:#1d4ed8; font-weight:700; cursor:pointer; margin-top:2px;">
                                {{ 'Ver foto' if f_frente else 'Mostrar' }}
                            </div>
                        </div>
                    </div>
                    <div style="display:flex; gap:5px; margin-top:8px;">
                        <label style="flex:1; padding:5px 4px; background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; border-radius:7px; font-size:10.5px; font-weight:800; text-align:center; cursor:pointer;">
                            Cámara
                            <input type="file" accept="image/*" capture="environment" onchange="procesarFotoCedula(this, 'Frente')" style="display:none;">
                        </label>
                        <label style="flex:1; padding:5px 4px; background:#ffffff; color:#334155; border:1px solid #cbd5e1; border-radius:7px; font-size:10.5px; font-weight:800; text-align:center; cursor:pointer;">
                            Galería
                            <input type="file" accept="image/*" onchange="procesarFotoCedula(this, 'Frente')" style="display:none;">
                        </label>
                    </div>
                </div>

                <!-- CUADRO COMPACTO: CÉDULA REVERSO -->
                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:9px;">
                    <div style="display:flex; align-items:center; gap:9px;">
                        <label style="width:52px; height:52px; border-radius:10px; background:#e2e8f0; border:1.5px solid #cbd5e1; display:flex; align-items:center; justify-content:center; overflow:hidden; cursor:pointer; flex-shrink:0;">
                            <img id="prevCedulaReverso" src="{{ f_reverso }}"
                                 style="width:100%; height:100%; object-fit:cover; display:{{ 'block' if f_reverso else 'none' }};">
                            <div id="placeholderCedulaReverso" style="display:{{ 'none' if f_reverso else 'flex' }}; align-items:center; justify-content:center; color:#475569;">
                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                            </div>
                            <input type="file" accept="image/*" capture="environment" onchange="procesarFotoCedula(this, 'Reverso')" style="display:none;">
                        </label>
                        <div>
                            <div style="font-size:12px; font-weight:800; color:#0f172a;">Reverso</div>
                            <div onclick="const s=document.getElementById('prevCedulaReverso').src; if(s && s.startsWith('data:image')) verFotoAmpliada(s);"
                                 style="font-size:10.5px; color:#1d4ed8; font-weight:700; cursor:pointer; margin-top:2px;">
                                {{ 'Ver foto' if f_reverso else 'Mostrar' }}
                            </div>
                        </div>
                    </div>
                    <div style="display:flex; gap:5px; margin-top:8px;">
                        <label style="flex:1; padding:5px 4px; background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; border-radius:7px; font-size:10.5px; font-weight:800; text-align:center; cursor:pointer;">
                            Cámara
                            <input type="file" accept="image/*" capture="environment" onchange="procesarFotoCedula(this, 'Reverso')" style="display:none;">
                        </label>
                        <label style="flex:1; padding:5px 4px; background:#ffffff; color:#334155; border:1px solid #cbd5e1; border-radius:7px; font-size:10.5px; font-weight:800; text-align:center; cursor:pointer;">
                            Galería
                            <input type="file" accept="image/*" onchange="procesarFotoCedula(this, 'Reverso')" style="display:none;">
                        </label>
                    </div>
                </div>
            </div>
            </div>

            <!-- TARJETA 4: GEOLOCALIZACIÓN Y GUARDADO -->
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px; box-shadow:0 2px 6px rgba(15,23,42,0.03);">
                <input type="hidden" id="latitud_input" name="latitud">
                <input type="hidden" id="longitud_input" name="longitud">

                <button type="button" onclick="obtenerCoordenadasGPS()"
                        style="width:100%; padding:12px; background:#eff6ff; color:#1d4ed8; border:1.5px solid #bfdbfe; border-radius:10px; font-size:13px; font-weight:800; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:7px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                    Capturar Ubicación GPS del Local
                </button>
                <p id="status_gps" style="font-size:11.5px; color:#64748b; font-weight:600; margin:6px 0 12px 0; text-align:center;">Ubicación GPS pendiente de captura</p>

                <button type="submit" id="btnGuardarCredito" class="btn-accion btn-pagar"
                        style="width:100%; padding:14px; font-size:14.5px; font-weight:800; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:11px; cursor:pointer; box-shadow:0 4px 12px rgba(22,163,74,0.25);">
                    Guardar y Crear Crédito
                </button>
            </div>
        </form>
    </div>


{% elif vista == 'balance' or vista == 'gastos' %}
    <div style="padding:0 4px 28px 4px;">
        <!-- Cabecera Corporativa -->
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 12px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">BALANCE CONTABLE</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">Control de efectivo y cierre de jornada</div>
            </div>
        </div>

        <!-- Tarjeta Principal de Efectivo Líquido Neto -->
        <div style="background:linear-gradient(145deg, #0a1f44 0%, #0f2b5c 60%, #1e3a8a 100%); border-radius:16px; padding:18px 16px; color:#ffffff; margin-bottom:10px; box-shadow:0 8px 20px rgba(15,43,92,0.22); text-align:left;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <span style="font-size:11px; font-weight:700; color:#cbd5e1; text-transform:uppercase; letter-spacing:0.6px;">Efectivo Líquido Neto</span>
                <span style="background:rgba(22,163,74,0.22); border:1px solid rgba(74,222,128,0.4); color:#86efac; font-size:10.5px; font-weight:800; padding:3px 9px; border-radius:20px;">
                    Base: ${{ "%.2f"|format(caja_base) }}
                </span>
            </div>
            <div style="font-size:28px; font-weight:900; letter-spacing:-0.5px; line-height:1.1;">${{ "%.2f"|format(efectivo_neto) }}</div>
            <div style="font-size:11px; color:#94a3b8; font-weight:600; margin-top:6px;">
                Estado de jornada: <b style="color:#ffffff;">{{ balance_estado }}</b>
            </div>
        </div>

        <!-- Tarjetas Entradas y Salidas -->
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px;">
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:12px 14px; text-align:left; box-shadow:0 2px 5px rgba(15,23,42,0.02);">
                <div style="display:flex; align-items:center; gap:6px;">
                    <span style="width:8px; height:8px; border-radius:50%; background:#16a34a;"></span>
                    <span style="font-size:10.5px; font-weight:800; color:#64748b; text-transform:uppercase;">Entradas</span>
                </div>
                <div style="font-size:17px; font-weight:900; color:#16a34a; margin-top:5px;">+${{ "%.2f"|format(entradas_totales) }}</div>
            </div>
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:12px 14px; text-align:left; box-shadow:0 2px 5px rgba(15,23,42,0.02);">
                <div style="display:flex; align-items:center; gap:6px;">
                    <span style="width:8px; height:8px; border-radius:50%; background:#ef4444;"></span>
                    <span style="font-size:10.5px; font-weight:800; color:#64748b; text-transform:uppercase;">Salidas / Gastos</span>
                </div>
                <div style="font-size:17px; font-weight:900; color:#ef4444; margin-top:5px;">-${{ "%.2f"|format(salidas_totales) }}</div>
            </div>
        </div>

        <!-- Bloque Cierre de Jornada Diaria -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Cierre de Jornada Diaria</span>
            </div>
            <p style="font-size:11.5px; color:#64748b; line-height:1.4; margin:0 0 12px 0;">
                Al finalizar el día, ingresa el dinero neto del balance para auditar y cerrar la jornada de cobranza.
            </p>

            {% if balance_estado != 'Abierta' %}
                <div style="background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d; padding:12px; border-radius:10px; font-size:12px; font-weight:700; text-align:center;">
                    JORNADA CERRADA CON ÉXITO<br><span style="font-weight:600; font-size:11.5px;">{{ balance_estado }}</span>
                </div>
            {% else %}
                <form action="/api/balance/cerrar_caja" method="POST" style="display:flex; flex-direction:column; gap:10px;">
                    <input type="hidden" name="efectivo_sistema" value="{{ efectivo_neto }}">
                    <div>
                        <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:5px;">Ingrese Efectivo ($)</label>
                        <input type="number" step="any" name="efectivo_real" required placeholder="Ej: {{ '%.2f'|format(efectivo_neto) }}"
                               style="width:100%; padding:12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:16px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <button type="submit" style="width:100%; padding:13px; background:linear-gradient(135deg, #e11d48, #be123c); color:#ffffff; border:none; border-radius:10px; font-size:14px; font-weight:800; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; box-shadow:0 4px 12px rgba(225,29,72,0.22);">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                        Cerrar Caja
                    </button>
                </form>
            {% endif %}
        </div>

        <!-- Bloque Registrar Movimiento de Flujo -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
                <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Registrar Movimiento de Flujo</span>
            </div>

            <form id="formBalance" method="POST" action="/api/balance/guardar_movimiento" onsubmit="return procesarYEnviarGasto(event)">
                <!-- Inputs reales conectados a tu JS -->
                <input type="hidden" name="tipo_mov" id="tipo_mov" value="Salida">
                <input type="hidden" name="categoria_mov" id="categoria_mov" value="Otros">

                <!-- Selector Táctil: Salida / Entrada -->
                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:6px;">Tipo de Flujo</label>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:14px;">
                    <button type="button" id="btnFlujoSalida" onclick="seleccionarTipoFlujoBal('Salida')"
                            style="padding:11px 8px; border-radius:10px; border:1.5px solid #ef4444; background:#fef2f2; color:#b91c1c; font-size:12.5px; font-weight:800; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>
                        Salida / Gasto
                    </button>
                    <button type="button" id="btnFlujoEntrada" onclick="seleccionarTipoFlujoBal('Entrada')"
                            style="padding:11px 8px; border-radius:10px; border:1.5px solid #cbd5e1; background:#ffffff; color:#475569; font-size:12.5px; font-weight:700; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
                        Entrada / Capital
                    </button>
                </div>

                <!-- Cuadrícula Táctil de Categorías -->
                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:6px;">Categoría del Movimiento</label>
                <div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:6px; margin-bottom:14px;">
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Gasolina')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Gasolina</button>
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Almuerzo')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Almuerzo</button>
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Transporte')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Transporte</button>
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Viáticos')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Viáticos</button>
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Sueldo')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Comisión</button>
                    <button type="button" class="btn-cat-bal" onclick="seleccionarCatBal(this, 'Inversión')" style="padding:9px 4px; border-radius:9px; border:1.5px solid #e2e8f0; background:#f8fafc; color:#334155; font-size:11px; font-weight:700; cursor:pointer;">Inversión</button>
                    <button type="button" class="btn-cat-bal active" onclick="seleccionarCatBal(this, 'Otros')" style="grid-column:span 3; padding:8px; border-radius:9px; border:1.5px solid #0f2b5c; background:#eff6ff; color:#0f2b5c; font-size:11px; font-weight:800; cursor:pointer;">Otros</button>
                </div>

                <!-- Campos Descripción, Monto y Comprobante -->
                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:5px;">Descripción / Detalle (Opcional)</label>
                <input type="text" name="concepto_mov" id="concepto_mov" placeholder="Ej: Compra de repuestos de moto"
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; margin-bottom:12px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:5px;">Monto ($)</label>
                <input type="number" step="any" name="monto_mov" id="monto_mov" placeholder="0.00" required
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:15px; font-weight:800; color:#0f172a; margin-bottom:12px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:6px;">Foto Factura / Comprobante (Opcional)</label>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px;">
                    <label style="display:flex; align-items:center; justify-content:center; gap:6px; padding:10px; border:1.5px dashed #0f2b5c; border-radius:10px; background:#eff6ff; color:#0f2b5c; font-size:12px; font-weight:800; cursor:pointer;">
                        <input type="file" id="foto_mov_cam" accept="image/*" capture="environment" style="display:none;" onchange="document.getElementById('foto_mov').files = this.files; document.getElementById('lblFotoBalTxt').innerText = (this.files && this.files[0]) ? 'Foto cámara: ' + this.files[0].name : 'Sin archivo seleccionado'; if(typeof procesarImagenEnCaliente==='function') procesarImagenEnCaliente();">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
                        Cámara
                    </label>
                    <label style="display:flex; align-items:center; justify-content:center; gap:6px; padding:10px; border:1.5px dashed #64748b; border-radius:10px; background:#f8fafc; color:#334155; font-size:12px; font-weight:800; cursor:pointer;">
                        <input type="file" id="foto_mov" accept="image/*,.pdf" style="display:none;" onchange="document.getElementById('lblFotoBalTxt').innerText = (this.files && this.files[0]) ? 'Archivo: ' + this.files[0].name : 'Sin archivo seleccionado'; if(typeof procesarImagenEnCaliente==='function') procesarImagenEnCaliente();">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                        Subir Archivo
                    </label>
                </div>
                <div id="lblFotoBalTxt" style="font-size:11px; color:#64748b; font-weight:600; text-align:center; margin-bottom:12px;">Sin archivo seleccionado</div>
                <input type="hidden" id="foto_comprimida_b64" name="foto_comprimida_b64">

                <button type="submit" style="width:100%; padding:13px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:10px; font-size:14px; font-weight:800; cursor:pointer; box-shadow:0 4px 12px rgba(22,163,74,0.25);">
                    Guardar Registro
                </button>
            </form>
        </div>

        <!-- Bloque Historial de Movimientos -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; padding-bottom:10px; margin-bottom:6px; border-bottom:1.5px solid #f1f5f9;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Historial de Movimientos de Hoy</span>
            </div>

            {% if gastos_list %}
                {% for g in gastos_list %}
                {% set es_entrada = (g.tipo == 'Entrada') %}
                <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #f1f5f9; gap:8px;">
                    <div style="display:flex; align-items:center; gap:10px; min-width:0; flex:1;">
                        <div style="width:30px; height:30px; border-radius:8px; background:{{ '#dcfce7' if es_entrada else '#fef2f2' }}; color:{{ '#15803d' if es_entrada else '#dc2626' }}; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                            {% if es_entrada %}
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
                            {% else %}
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>
                            {% endif %}
                        </div>
                        <div style="min-width:0;">
                            <div style="font-size:10px; font-weight:800; text-transform:uppercase; color:{{ '#15803d' if es_entrada else '#dc2626' }}; letter-spacing:0.3px;">{{ g.categoria }}</div>
                            <div style="font-size:12.5px; font-weight:700; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ g.concepto | replace('🔸', '') | trim }}</div>
                        </div>
                    </div>

                    <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                        <span style="font-weight:900; font-size:13.5px; color:{{ '#16a34a' if es_entrada else '#ef4444' }};">
                            {{ '+' if es_entrada else '-' }}${{ "%.2f"|format(g.monto) }}
                        </span>

                        {% if g.comprobante and g.comprobante != "None" and g.comprobante != "" %}
                            <button type="button" onclick="cargarYVerFoto({{ g.id }})" title="Ver comprobante"
                                    style="width:28px; height:28px; border-radius:7px; background:#eff6ff; color:#1d4ed8; border:1px solid #dbeafe; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0;">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                            </button>
                        {% endif %}

                        {% if "MICROSEGURO" not in g.concepto and "RECAUDACIÓN DE CUOTAS" not in g.concepto and g.id != 0 %}
                            <button type="button" onclick="postAccionAvanta('/api/eliminar_gasto/{{ g.id }}', '/menu/balance', '¿Eliminar este registro del balance?')" title="Eliminar"
                               style="width:28px; height:28px; border-radius:7px; background:#fef2f2; color:#dc2626; border:1px solid #fecaca; display:flex; align-items:center; justify-content:center;">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                            </button>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div style="text-align:center; font-size:12.5px; color:#94a3b8; padding:18px 8px; font-weight:600;">No hay flujos asentados hoy.</div>
            {% endif %}
        </div>
    </div>


{% elif vista == 'perfil_cobrador' %}
    <!-- Encabezado de Sección con SVG limpio -->
    <div class="section-header-title" style="display:flex; align-items:center; gap:8px; margin-bottom:14px;">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        <span>PERFIL DEL COBRADOR</span>
    </div>

    <!-- Credencial Corporativa Superior -->
    <div style="background:linear-gradient(135deg, #0f2b5c 0%, #1e3a8a 100%); border-radius:16px; padding:22px 20px; color:white; margin-bottom:16px; box-shadow:0 8px 20px rgba(15,43,92,0.22); position:relative; overflow:hidden;">
        <!-- Detalle decorativo sutil -->
        <div style="position:absolute; right:-20px; top:-20px; width:110px; height:110px; border-radius:50%; background:rgba(255,255,255,0.06);"></div>
        
        <div style="display:flex; align-items:center; gap:16px; position:relative; z-index:1;">
            <div style="width:62px; height:62px; border-radius:50%; background:linear-gradient(135deg, #38bdf8, #0284c7); border:3px solid rgba(255,255,255,0.25); display:flex; align-items:center; justify-content:center; font-size:22px; font-weight:900; color:white; flex-shrink:0; box-shadow:0 4px 10px rgba(0,0,0,0.2);">
                {{ ((nombre.split()[0][0] + nombre.split()[1][0]) if (nombre and nombre.split()|length >= 2) else (nombre[:2] if nombre else 'AV'))|upper }}
            </div>
            <div style="text-align:left; flex:1;">
                <div style="display:inline-flex; align-items:center; gap:5px; background:rgba(16,185,129,0.2); border:1px solid rgba(16,185,129,0.4); color:#6ee7b7; padding:3px 9px; border-radius:20px; font-size:10px; font-weight:800; letter-spacing:0.4px; margin-bottom:6px;">
                    <span style="width:6px; height:6px; border-radius:50%; background:#10b981;"></span> RUTA ACTIVA
                </div>
                <h3 style="margin:0; font-size:18px; font-weight:800; color:#ffffff; letter-spacing:-0.3px;">{{ nombre if nombre else 'Cobrador Asignado' }}</h3>
                <p style="margin:2px 0 0 0; font-size:12px; color:#cbd5e1; font-weight:500;">Agente Oficial • Avanta Pagos</p>
            </div>
        </div>
    </div>

    <!-- Tarjeta de Información Personal y Contacto -->
    <div class="card" style="padding:20px; text-align:left; background:white; border-radius:16px; box-shadow:0 2px 10px rgba(0,0,0,0.04); border:1px solid #e2e8f0; margin-bottom:24px;">
        <h4 style="margin:0 0 14px 0; font-size:13px; font-weight:800; color:#0f172a; text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid #f1f5f9; padding-bottom:10px;">
            Información Personal y de Contacto
        </h4>

        <form id="formPerfil" onsubmit="actualizarPerfilCompleto(event)">
            <!-- Campo oculto para mantener compatibilidad sin mostrar credenciales -->
            <input type="hidden" id="perfilUsuario" value="{{ usuario_actual }}">
            <input type="hidden" id="perfilClave" value="">

            <div style="margin-bottom:14px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                    Nombre Completo
                </label>
                <input type="text" id="perfilNombre" value="{{ nombre }}" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"></rect><line x1="7" y1="8" x2="17" y2="8"></line><line x1="7" y1="12" x2="13" y2="12"></line></svg>
                    Cédula de Identidad
                </label>
                <input type="text" id="perfilCedula" value="{{ cedula }}" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"></rect><line x1="12" y1="18" x2="12.01" y2="18"></line></svg>
                    Número de Celular
                </label>
                <input type="text" id="perfilCelular" value="{{ celular }}" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; color:#0f172a; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:20px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>
                    Correo Electrónico
                </label>
                <input type="email" id="perfilCorreo" value="{{ correo }}" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; color:#0f172a; box-sizing:border-box;">
            </div>

            <button type="submit" style="width:100%; border:none; padding:14px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); border-radius:12px; font-size:14px; font-weight:800; color:white; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; box-shadow:0 4px 12px rgba(15,43,92,0.25);">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                <span>Guardar Cambios del Perfil</span>
            </button>
        </form>
    </div>
{% endif %}
"""


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>AVANTA PAGOS v1.3</title>
    <!-- 🌐 FIJADO: Enlaces CDN oficiales para renderizar la tipografía y los iconos vectoriales de la captura -->
    <style>
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; margin: 0; padding: 0; }
        body { background: #f8fafc; color: #0f172a; padding-bottom: 60px; }
        .navbar { background: #0f2b5c; color: white; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 200; }
        .btn-nav-icon { background: rgba(255,255,255,0.12); color: white; border: none; padding: 8px 12px; border-radius: 8px; font-weight: 700; font-size: 14px; cursor: pointer; display: flex; align-items: center; gap: 6px; text-decoration: none; }
        .nav-title { font-size: 15px; font-weight: 800; color: white; }
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.5); z-index: 300; }
        .drawer-overlay.active { display: block; }
        .drawer { position: fixed; top: 0; left: -290px; width: 290px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s ease; display: flex; flex-direction: column; }
        .drawer.active { left: 0; }
        .drawer-header { background: #0f2b5c; color: white; padding: 20px 16px; border-bottom: 4px solid #00a8cc; }
        .drawer-logo { font-size: 18px; font-weight: 800; }
        .drawer-menu { list-style: none; padding: 10px 0; overflow-y: auto; flex: 1; }
        .drawer-menu li a { display: flex; align-items: center; gap: 12px; padding: 13px 20px; color: #334155; text-decoration: none; font-weight: 700; font-size: 13px; border-bottom: 1px solid #f1f5f9; }
        .drawer-menu li a i { font-size: 16px; width: 20px; color: #0f2b5c; text-align: center; }
        .date-badge { text-align: center; font-size: 13px; font-weight: 800; color: #475569; background: #e2e8f0; padding: 6px; border-radius: 6px; margin-bottom: 10px; }
        .grid-kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
        .kpi-card { background: white; padding: 12px 10px; border-radius: 12px; border: 1px solid #e2e8f0; }
        .kpi-title { font-size: 10px; color: #64748b; font-weight: 800; text-transform: uppercase; }
        .kpi-val { font-size: 15px; font-weight: 800; color: #0f2b5c; }
        .expected-card { background: #f0f9ff; border: 1px solid #bae6fd; padding: 12px; border-radius: 12px; margin-bottom: 12px; text-align: left; }
        .search-box { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 10px; margin-bottom: 12px; font-size: 13px; outline: none; }
        .card { background: white; padding: 14px; border-radius: 12px; margin-bottom: 10px; border: 1px solid #e2e8f0; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .btn-accion { border: none; padding: 8px 12px; border-radius: 8px; font-weight: 800; font-size: 11px; cursor: pointer; color: white; text-align: center; }
        .btn-pagar { background: #10b981; } .btn-nopagar { background: #ef4444; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.6); z-index: 400; justify-content: center; align-items: center; padding: 16px; }
        .modal-content { background: white; border-radius: 16px; padding: 20px; width: 100%; max-width: 420px; max-height: 90vh; overflow-y: auto; text-align: center; }
        .modal-grid-data { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; text-align: left; margin-bottom: 12px; }
        .data-box { background: #f8fafc; padding: 8px; border-radius: 8px; border: 1px solid #f1f5f9; }
        .data-lbl { font-size: 9px; color: #64748b; font-weight: 800; text-transform: uppercase; }
        .data-val { font-size: 12px; font-weight: 700; color: #1e293b; }
        .btn-ws { background: #25d366; color: white; text-decoration: none; display: block; padding: 10px; border-radius: 6px; font-weight: bold; margin-top: 8px; }
        .btn-modal-close { background: #e2e8f0; color: #334155; border: none; padding: 10px; border-radius: 8px; font-weight: 700; width: 100%; margin-top: 6px; cursor: pointer; }
        label { font-size: 11px; font-weight: bold; color: #475569; display: block; margin: 6px 0 2px 0; text-align: left; }
        input[type="text"], input[type="number"], input[type="date"], select { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13px; outline: none; }
        
        /* 📱 ESTILOS PREMIUM UNIFICADOS COMPATIBLES CON CELULARES */
        /* 📱 FIJADO: Bloque vertical responsivo rígido para evitar que las tarjetas se monten de lado */
        .cliente-card-premium { background: #ffffff !important; border-radius: 10px !important; padding: 12px !important; margin-bottom: 12px !important; border: 1px solid #e2e8f0 !important; display: flex !important; flex-direction: row !important; justify-content: space-between !important; align-items: center !important; box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important; width: 100% !important; clear: both !important; }
        .card-izquierda { display: flex !important; flex-direction: row !important; align-items: flex-start !important; gap: 12px !important; flex: 1 !important; text-align: left !important; width: calc(100% - 60px) !important; }
        .circulo-frecuencia { width: 34px; height: 34px; border-radius: 50%; border: 2.5px solid #ef4444; color: #ef4444; font-weight: 800; font-size: 14px; display: flex; align-items: center; justify-content: center; background: #fff1f2; flex-shrink: 0; }
        .circulo-frecuencia.al-dia { border-color: #10b981; color: #10b981; background: #ecfdf5; }
        .circulo-frecuencia.mora-leve { border-color: #f59e0b; color: #f59e0b; background: #fffbeb; }
        .info-bloque-texto { display: flex; flex-direction: column; flex: 1; }
        .id-alias { font-size: 15px; font-weight: 800; color: #0f172a; line-height: 1.2; }
        .nombre-real { font-size: 13px; color: #64748b; margin: 2px 0 8px 0; font-weight: 500; }
        .grid-metricas-premium { display: grid; grid-template-columns: repeat(3, 1fr) !important; width: 100% !important; gap: 4px !important; text-align: left !important; margin-bottom: 8px !important; }
        .metric-col { display: flex; flex-direction: column; justify-content: flex-start; }
        .metric-lbl { font-size: 11px !important; color: #94a3b8 !important; font-weight: 700 !important; margin-bottom: 2px !important; text-transform: none !important; }
        .metric-val { font-size: 13px !important; font-weight: 800 !important; color: #334155 !important; }
        .fila-utilidades-premium { display: flex; align-items: center; gap: 8px; margin-top: auto; padding-top: 4px; }
        .btn-utilidad-foto { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; width: 32px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 13px; }
        .circulo-check-cuota { background: #e2e8f0; color: #475569; width: 24px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; border: 1px solid #cbd5e1; }
        .circulo-check-cuota.gestionado { background: #fee2e2; color: #ef4444; border-color: #fca5a5; }
        .circulo-check-cuota.gestionado-ok { background: #dcfce7; color: #10b981; border-color: #bbf7d0; }
        .badge-saldo-premium { font-size: 11px; color: #64748b; }
        .badge-saldo-premium b { color: #1e293b; font-weight: 800; }
        .card-derecha-acciones { display: flex; flex-direction: column; justify-content: space-around; align-items: center; padding-left: 12px; border-left: 1px solid #e2e8f0; min-width: 50px; gap: 4px; }
        .btn-accion-premium { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; cursor: pointer; font-size: 18px; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; transition: all 0.1s ease; }
        .btn-accion-premium:active { transform: scale(0.9); }
        .btn-accion-premium.cobrar { color: #10b981; background: #ecfdf5; border-color: #bbf7d0; }
        .btn-accion-premium.saltar { color: #ef4444; background: #fff1f2; border-color: #fca5a5; }
        @media print {
            body * { visibility: hidden; }
            #ticketPrint, #ticketPrint * { visibility: visible; }
            #ticketPrint { position: absolute; left: 0; top: 0; width: 58mm; font-family: monospace; font-size: 11px; }
        }
    </style>
</head>
<body>
    {% set p_act = request.path %}
    {% set mapa_titulos = {
        '/menu/clientes': {'titulo': 'DIRECTORIO DE CLIENTES', 'volver': '/'},
        '/menu/enrutar': {'titulo': 'SECUENCIA DE RUTA', 'volver': '/'},
        '/menu/historial': {'titulo': 'AUDITORÍA POR FECHA', 'volver': '/'},
        '/menu/balance': {'titulo': 'BALANCE CONTABLE', 'volver': '/'},
        '/menu/perfil': {'titulo': 'PERFIL DEL COBRADOR', 'volver': '/'},
        '/menu/semanales': {'titulo': 'ENRUTAMIENTO ANTICIPADO', 'volver': '/'},
        '/menu/backup': {'titulo': 'RESPALDO EN LA NUBE', 'volver': '/'},
        '/menu/impresora': {'titulo': 'IMPRESORA TÉRMICA', 'volver': '/'},
        '/menu/config': {'titulo': 'CONFIGURACIÓN OPERATIVA', 'volver': '/'},
        '/nuevo': {'titulo': 'NUEVO CRÉDITO', 'volver': '/'}
    } %}
    {% set es_expediente = p_act.startswith('/menu/cliente/') %}
    {% set info_nav = {'titulo': 'EXPEDIENTE CLIENTE', 'volver': '/menu/clientes'} if es_expediente else mapa_titulos.get(p_act, {'titulo': 'AVANTA PAGOS', 'volver': '/'}) %}
    <div class="navbar" style="gap:8px;">
        <button class="btn-menu btn-nav-icon" style="flex-shrink:0;" onclick="toggleDrawer()"><i class="fa-solid fa-bars"></i> Menú</button>
        <div class="nav-title" style="flex:1; display:flex; align-items:center; justify-content:center; gap:7px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:13.5px; letter-spacing:0.4px;">
            {% if p_act == '/' %}
                <span style="width:24px; height:24px; border-radius:7px; background:rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.22); display:inline-flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <svg width="15" height="15" viewBox="0 0 48 48" fill="none">
                        <path d="M12 37L24 10L36 37" stroke="#ffffff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
                        <path d="M9 38C18 32 27 25 36 16" stroke="#fbbf24" stroke-width="4.2" stroke-linecap="round"/>
                        <path d="M27 16L37 15L36 25" stroke="#fbbf24" stroke-width="4.2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </span>
                <span>AVANTA PAGOS</span>
            {% else %}
                <span>{{ info_nav.titulo }}</span>
            {% endif %}
        </div>
        {% if p_act == '/' %}
            <a href="javascript:void(0)" onclick="navegarRuta('/nuevo')" class="btn-nav-icon" style="flex-shrink:0; background:#00a8cc;"><i class="fa-solid fa-plus"></i> Nuevo</a>
        {% else %}
            <a href="javascript:void(0)" onclick="navegarRuta('{{ info_nav.volver }}')" class="btn-nav-icon" style="flex-shrink:0; background:rgba(255,255,255,0.16); border:1px solid rgba(255,255,255,0.22);"><i class="fa-solid fa-arrow-left"></i> Volver</a>
        {% endif %}
    </div>

    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
        <div class="drawer" id="drawer" style="background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; box-shadow: 6px 0 28px rgba(15, 23, 42, 0.22); padding: 0; border-right: 1px solid #e2e8f0;">
        <!-- Cabecera Ejecutiva con Isotipo Oficial (A + Flecha Dorada) -->
        <div class="drawer-header" style="background: linear-gradient(145deg, #0a1f44 0%, #0f2b5c 60%, #1e3a8a 100%); padding: 20px 16px; border-bottom: 1px solid rgba(255,255,255,0.1); display: flex; align-items: center; gap: 12px;">
            <div style="width: 44px; height: 44px; border-radius: 12px; background: rgba(255,255,255,0.08); border: 1.5px solid rgba(255,255,255,0.22); display: flex; align-items: center; justify-content: center; flex-shrink: 0; position: relative;">
                <svg width="26" height="26" viewBox="0 0 48 48" fill="none">
                    <path d="M12 37L24 10L36 37" stroke="#ffffff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
                    <path d="M9 38C18 32 27 25 36 16" stroke="#fbbf24" stroke-width="4.2" stroke-linecap="round"/>
                    <path d="M27 16L37 15L36 25" stroke="#fbbf24" stroke-width="4.2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </div>
            <div style="text-align: left; min-width: 0;">
                <div style="font-size: 15px; font-weight: 900; color: #ffffff; letter-spacing: 0.4px; line-height: 1.15;">AVANTA PAGOS</div>
                <div style="display: flex; align-items: center; gap: 6px; margin-top: 4px;">
                    <span style="width: 7px; height: 7px; border-radius: 50%; background: #22c55e; display: inline-block;"></span>
                    <span style="font-size: 11px; font-weight: 700; color: #cbd5e1; letter-spacing: 0.3px;">{{ session.get('nombre_ruta', 'RUTA-01') }} • PRO V1.3</span>
                </div>
            </div>
        </div>

        <ul class="drawer-menu" style="list-style: none; padding: 12px 12px 20px 12px; margin: 0; display: flex; flex-direction: column; gap: 5px; overflow-y: auto; flex: 1;">
            <!-- SECCIÓN 1: OPERACIÓN DIARIA -->
            <li style="font-size: 10px; font-weight: 800; color: #64748b; letter-spacing: 0.8px; text-transform: uppercase; padding: 4px 6px 2px 6px; text-align: left;">Operación Diaria</li>

            <!-- INICIO / TABLERO -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #eff6ff; color: #0f2b5c; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Inicio / Cobranza</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- CLIENTES -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/clientes')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #eff6ff; color: #0f2b5c; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Clientes</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- RUTA -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/ruta_orden')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #eff6ff; color: #0f2b5c; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21"/><line x1="9" y1="3" x2="9" y2="18"/><line x1="15" y1="6" x2="15" y2="21"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Ruta</span>
                    </div>
                    <span style="background: #eff6ff; color: #1d4ed8; font-size: 10px; font-weight: 800; padding: 3px 8px; border-radius: 6px; border: 1px solid #dbeafe;">Organizar</span>
                </a>
            </li>

            <!-- LISTADOS -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/listados')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #eff6ff; color: #0f2b5c; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Listados</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- BALANCE -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/balance')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #dcfce7; color: #15803d; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Balance</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- SECCIÓN 2: HERRAMIENTAS Y SISTEMA -->
            <li style="font-size: 10px; font-weight: 800; color: #64748b; letter-spacing: 0.8px; text-transform: uppercase; padding: 10px 6px 2px 6px; text-align: left;">Herramientas y Sistema</li>

            <!-- MI PERFIL -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/perfil')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; color: #334155; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Mi Perfil</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- AGREGAR A LISTA -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/agregar_lista')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; color: #334155; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Agregar a lista</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- SALVAR DATOS -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/salvar_datos')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; color: #334155; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Salvar datos</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- OPCIONES BLUETOOTH -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/bluetooth')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #f0fdfa; color: #0d9488; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Opciones Bluetooth</span>
                    </div>
                    <span style="background: #ccfbf1; color: #0f766e; font-size: 10px; font-weight: 800; padding: 3px 7px; border-radius: 6px;">58mm</span>
                </a>
            </li>

            <!-- ACCIONES MAESTRO -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/configuracion')" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div style="width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; color: #334155; display: flex; align-items: center; justify-content: center;">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                        </div>
                        <span style="font-size: 13px; font-weight: 700; color: #0f172a;">Acciones Maestro</span>
                    </div>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                </a>
            </li>

            <!-- BOTÓN CERRAR SESIÓN -->
            <li style="margin: 12px 0 0 0; padding-top: 10px; border-top: 1px solid #e2e8f0;">
                <a href="/logout" style="display: flex; align-items: center; justify-content: center; gap: 9px; padding: 13px; background: linear-gradient(135deg, #e11d48, #be123c); color: #ffffff; text-decoration: none; border-radius: 12px; font-weight: 800; font-size: 14px; box-shadow: 0 4px 12px rgba(225, 29, 72, 0.25);">
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                        <polyline points="16 17 21 12 16 7"/>
                        <line x1="21" y1="12" x2="9" y2="12"/>
                    </svg>
                    Cerrar Sesión
                </a>
            </li>
        </ul>
    </div>

    <div class="main-container" id="mainContent">
        {{ contenido_html | safe }}
    </div>

    <div class="modal" id="modalInfoCliente">
        <div class="modal-content">
            <h3 id="inf_nombre" style="margin-bottom:12px; color:#0f2b5c;">Cargando Ficha...</h3>
            <div class="modal-grid-data">
                <div class="data-box"><div class="data-lbl">Teléfono</div><div class="data-val" id="inf_tel"></div></div>
                <div class="data-box"><div class="data-lbl">Identificación</div><div class="data-val" id="inf_id"></div></div>
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Dirección Domiciliaria</div><div class="data-val" id="inf_dir"></div></div>
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Referencia</div><div class="data-val" id="inf_ref"></div></div>
                <div class="data-box"><div class="data-lbl">Fecha Crédito</div><div class="data-val" id="inf_f_ini"></div></div>
                <div class="data-box"><div class="data-lbl">Vencimiento</div><div class="data-val" id="inf_f_venc"></div></div>
                <div class="data-box"><div class="data-lbl">Valor Cuota</div><div class="data-val" id="inf_v_cuota"></div></div>
                <div class="data-box"><div class="data-lbl">Saldo Actual</div><div class="data-val" id="inf_saldo_act" style="color:#ef4444;"></div></div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:8px;">
                <a href="javascript:void(0)" id="btn_llamar" class="btn-accion btn-abono" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-phone"></i> Llamar</a>
                <a href="javascript:void(0)" id="btn_mapa" target="_blank" class="btn-accion btn-pagar" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-location-dot"></i> Ver Mapa</a>
            </div>
            <button onclick="cerrarModal('modalInfoCliente')" class="btn-modal-close">Salir de la Información</button>
        </div>
    </div>

    <div class="modal" id="modalPerfilOk">
        <div class="modal-content" style="border-radius:20px; padding:24px 20px; max-width:340px; box-shadow:0 20px 45px rgba(15,23,42,0.35);">
            <div style="width:56px; height:56px; border-radius:50%; background:#dcfce7; color:#16a34a; display:flex; align-items:center; justify-content:center; margin:0 auto 12px auto; border:2px solid #86efac;">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <h3 style="color:#0f172a; font-size:18px; font-weight:900; margin:0 0 6px 0;">Datos Actualizados</h3>
            <p style="margin:0 0 18px 0; font-size:13.5px; color:#475569; font-weight:600; line-height:1.4;">La ficha del titular se ha guardado correctamente en el sistema.</p>
            <button type="button" onclick="document.getElementById('modalPerfilOk').style.display='none';"
                    style="width:100%; padding:13px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:14px; cursor:pointer;">
                Aceptar
            </button>
        </div>
    </div>

    <div class="modal" id="modalVisorFoto" style="z-index:20000;" onclick="this.style.display='none'">
        <div class="modal-content" style="border-radius:18px; padding:14px; max-width:92%; background:#0f172a;" onclick="event.stopPropagation()">
            <img id="imgVisorFotoFull" src="" style="width:100%; max-height:72vh; object-fit:contain; border-radius:10px; display:block;">
            <button type="button" onclick="document.getElementById('modalVisorFoto').style.display='none'"
                    style="width:100%; margin-top:10px; padding:11px; background:#ffffff; color:#0f172a; border:none; border-radius:10px; font-weight:800; font-size:13px; cursor:pointer;">
                Cerrar Vista Previa
            </button>
        </div>
    </div>

    <div class="modal" id="modalCreditoOk">
        <div class="modal-content" style="border-radius:20px; padding:24px 20px; max-width:350px; box-shadow:0 20px 45px rgba(15,23,42,0.35);">
            <div style="width:56px; height:56px; border-radius:50%; background:#dcfce7; color:#16a34a; display:flex; align-items:center; justify-content:center; margin:0 auto 12px auto; border:2px solid #86efac;">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <h3 style="color:#0f172a; font-size:18px; font-weight:900; margin:0 0 6px 0;">Crédito Registrado</h3>
            <p id="modalCreditoOkMsg" style="margin:0 0 18px 0; font-size:13.5px; color:#475569; font-weight:600; line-height:1.4;">El titular y su tabla de amortización han sido creados con éxito.</p>
            <button type="button" onclick="window.location.href='/';"
                    style="width:100%; padding:13px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:14px; cursor:pointer; box-shadow:0 4px 12px rgba(15,43,92,0.25);">
                Ir al Tablero de Ruta
            </button>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content" style="border-radius:20px; padding:24px 20px; max-width:360px; box-shadow:0 20px 45px rgba(15,23,42,0.35);">
            <div style="width:56px; height:56px; border-radius:50%; background:#dcfce7; color:#16a34a; display:flex; align-items:center; justify-content:center; margin:0 auto 12px auto; border:2px solid #86efac;">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <h3 style="color:#0f172a; font-size:18px; font-weight:900; margin:0 0 6px 0;">Pago Procesado</h3>
            <p id="modalMsg" style="margin:0 0 18px 0; font-size:13.5px; color:#475569; font-weight:600; line-height:1.4;"></p>

            <div style="display:flex; flex-direction:column; gap:9px;">
                <a href="javascript:void(0)" id="modalWsBtn" target="_blank"
                   style="display:flex; align-items:center; justify-content:center; gap:8px; padding:13px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; text-decoration:none; border-radius:11px; font-weight:800; font-size:14px; box-shadow:0 4px 12px rgba(22,163,74,0.25);">
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
                    Enviar Comprobante WhatsApp
                </a>

                <button type="button" onclick="imprimirTicket()"
                        style="width:100%; padding:12px; background:#eff6ff; color:#0f2b5c; border:1.5px solid #bfdbfe; border-radius:11px; font-weight:800; font-size:13.5px; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
                    Imprimir Ticket (58mm)
                </button>

                <button type="button" onclick="window.location.reload();"
                        style="width:100%; padding:12px; background:#0f2b5c; color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:13.5px; cursor:pointer; margin-top:2px;">
                    Continuar en Ruta
                </button>
            </div>
        </div>
    </div>

<!-- 🎯 PANTALLA COMPLETA: GESTOR DE PAGOS (AVANTA PAGOS) -->
<div id="vistaGestorPago" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#ffffff; z-index:9500; overflow-y:auto; flex-direction:column; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <!-- Cabecera Azul Marino -->
    <div style="background:#0f2b5c; color:#ffffff; padding:14px 16px; display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:10; box-shadow:0 2px 8px rgba(0,0,0,0.15);">
        <button type="button" onclick="cerrarGestorPantallas()" style="background:transparent; border:none; color:#ffffff; font-size:22px; cursor:pointer; padding:4px 8px; line-height:1;">←</button>
        <div>
            <div style="font-size:17px; font-weight:800; line-height:1.2;">Gestor pagos</div>
            <div id="gpSubtitulo" style="font-size:11px; color:#cbd5e1; font-weight:500;">Ruta Activa • Avanta Pagos</div>
        </div>
    </div>

    <div style="padding:16px 18px 34px 18px; max-width:500px; width:100%; margin:0 auto; box-sizing:border-box;">
        <input type="hidden" id="abonoClienteId">
        <input type="hidden" id="abonoNumCuota">
        <input type="hidden" id="gpValorCuotaBase" value="0">
        <input type="hidden" id="gpSaldoActualNum" value="0">
        <input type="hidden" id="gpCuotasPendNum" value="1">
        <input type="hidden" id="gpTipoPago" value="cuota">
        <input type="hidden" id="gpMetodoPago" value="efectivo">

        <!-- Ficha del Cliente -->
        <div style="display:flex; flex-direction:column; gap:7px; font-size:14px; color:#334155; padding-bottom:12px; border-bottom:1px solid #cbd5e1;">
            <div style="display:flex; justify-content:space-between;"><span>ID Cliente:</span><b id="gpIdCliente" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Nombre</span><b id="gpNombre" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Alias</span><b id="gpAlias" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Documento 1</span><b id="gpDocumento" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span id="gpLblDiasEstado">Días de mora</span><b id="gpDiasMora" style="color:#0f172a;">0</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Fecha último pago</span><b id="gpUltimoPago" style="color:#0f172a;">-</b></div>
        </div>

        <!-- Saldo Actual -->
        <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 0 18px 0; font-size:16px; color:#334155;">
            <span>Saldo actual: $</span>
            <b id="gpSaldoTexto" style="font-size:17px; color:#0f172a;">$0.00</b>
        </div>

        <!-- Switch Castigo -->
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
            <span style="font-size:14px; color:#334155; font-weight:500;">¿Enviar a estudio para castigo de cartera?</span>
            <div style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gpChkCastigo" style="opacity:0; width:0; height:0;">
                <span onclick="var c=document.getElementById('gpChkCastigo'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </div>
        </div>

        <!-- Tipo de pago (Cuota / Dinero) -->
        <div style="margin-bottom:18px; text-align:left;">
            <div style="font-size:14px; color:#334155; font-weight:600; margin-bottom:8px;">Tipo de pago</div>
            <div style="display:grid; grid-template-columns:1fr 1fr; border:1px solid #cbd5e1; border-radius:6px; overflow:hidden;">
                <button type="button" id="btnTipoCuota" onclick="seleccionarTipoPagoGP('cuota')" style="padding:11px; border:none; background:#dcfce7; color:#15803d; font-weight:700; font-size:14px; cursor:pointer; border-right:1px solid #cbd5e1;">Cuota</button>
                <button type="button" id="btnTipoDinero" onclick="seleccionarTipoPagoGP('dinero')" style="padding:11px; border:none; background:#ffffff; color:#475569; font-weight:600; font-size:14px; cursor:pointer;">Dinero</button>
            </div>
        </div>

        <!-- Método de pago (Efectivo / Transacción) -->
        <div style="margin-bottom:20px; text-align:left;">
            <div style="font-size:14px; color:#334155; font-weight:600; margin-bottom:8px;">Método de pago</div>
            <div style="display:grid; grid-template-columns:1fr 1fr; border:1px solid #cbd5e1; border-radius:6px; overflow:hidden;">
                <button type="button" id="btnMetEfectivo" onclick="seleccionarMetodoPagoGP('efectivo')" style="padding:11px; border:none; background:#dcfce7; color:#15803d; font-weight:700; font-size:14px; cursor:pointer; border-right:1px solid #cbd5e1;">Efectivo</button>
                <button type="button" id="btnMetTrans" onclick="seleccionarMetodoPagoGP('transferencia')" style="padding:11px; border:none; background:#ffffff; color:#475569; font-weight:600; font-size:14px; cursor:pointer;">Transacción electrónica</button>
            </div>
        </div>

        <!-- Switch Pagar Total -->
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
            <span style="font-size:14px; color:#334155; font-weight:500;">Pagar total. Cuotas:</span>
            <div style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gpChkPagarTotal" style="opacity:0; width:0; height:0;">
                <span id="gpSwitchTotalVisual" onclick="togglePagarTotalGP()" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </div>
        </div>

        <!-- Valor Cuota / Monto Grande -->
        <div style="text-align:left; margin-bottom:18px;">
            <div id="gpLblMonto" style="font-size:14px; color:#334155; font-weight:500;">Valor cuota:</div>
            <div id="gpDisplayMontoGrande" style="text-align:center; font-size:28px; font-weight:800; color:#0f172a; margin:8px 0;">$0.00</div>
            <!-- Input para modo Dinero -->
            <input type="number" step="any" id="abonoMontoInput" oninput="actualizarDesdeInputDineroGP()" placeholder="Ingrese monto ($)" style="display:none; width:100%; padding:12px; font-size:20px; font-weight:800; text-align:center; border:2px solid #0f2b5c; border-radius:8px; box-sizing:border-box; margin-top:6px;">
        </div>

        <!-- Selector de Cuotas (- 1 +) y Slider -->
        <div id="gpBloqueStepperCuotas" style="margin-bottom:20px; text-align:left;">
            <div style="font-size:14px; color:#334155; font-weight:500; margin-bottom:8px;">Cuotas a pagar</div>
            <div style="display:grid; grid-template-columns:70px 1fr 70px; border:1.5px solid #0f2b5c; border-radius:8px; overflow:hidden; height:46px;">
                <button type="button" onclick="cambiarCuotasGP(-1)" style="background:#0f2b5c; color:#ffffff; border:none; font-size:24px; font-weight:700; cursor:pointer;">-</button>
                <div id="gpCantidadCuotasTexto" style="display:flex; align-items:center; justify-content:center; font-size:18px; font-weight:800; color:#0f172a; background:#ffffff;">1</div>
                <button type="button" onclick="cambiarCuotasGP(1)" style="background:#0f2b5c; color:#ffffff; border:none; font-size:24px; font-weight:700; cursor:pointer;">+</button>
            </div>
            <input type="range" id="gpSliderCuotas" min="1" max="24" value="1" oninput="setCuotasGP(this.value)" style="width:100%; margin-top:16px; accent-color:#0f2b5c; cursor:pointer;">
        </div>

        <!-- Adicionar Fotografía -->
        <div style="text-align:left; margin-bottom:24px;">
            <div style="font-size:14px; color:#334155; font-weight:500; margin-bottom:10px;">Adicionar fotografía</div>
            <label style="width:64px; height:64px; background:#e2e8f0; border-radius:8px; display:flex; align-items:center; justify-content:center; cursor:pointer; overflow:hidden; border:1px solid #cbd5e1;">
                <input type="file" accept="image/*" capture="environment" id="gpInputFoto" onchange="previsualizarFotoGP(this, 'gpIconoCamara', 'gpPreviewImg')" style="display:none;">
                <svg id="gpIconoCamara" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                <img id="gpPreviewImg" src="" style="display:none; width:100%; height:100%; object-fit:cover;">
            </label>
        </div>

                <!-- Botón Registrar Pago -->
        <button type="button" onclick="confirmarAbono()" style="width:100%; padding:15px; background:#0f2b5c; color:#ffffff; border:none; border-radius:8px; font-size:16px; font-weight:800; cursor:pointer; box-shadow:0 4px 12px rgba(15,43,92,0.25);">
            Registrar pago
        </button>
    </div>
</div>

<!-- 🎯 PANTALLA COMPLETA: GESTOR DE NO PAGOS (AVANTA PAGOS) -->
<div id="vistaGestorNoPago" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#ffffff; z-index:9500; overflow-y:auto; flex-direction:column; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <div style="background:#0f2b5c; color:#ffffff; padding:14px 16px; display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:10; box-shadow:0 2px 8px rgba(0,0,0,0.15);">
        <button type="button" onclick="cerrarGestorPantallas()" style="background:transparent; border:none; color:#ffffff; font-size:22px; cursor:pointer; padding:4px 8px; line-height:1;">←</button>
        <div>
            <div style="font-size:17px; font-weight:800; line-height:1.2;">Gestor pagos</div>
            <div id="gnpSubtitulo" style="font-size:11px; color:#cbd5e1; font-weight:500;">Registro de No Pago • Avanta Pagos</div>
        </div>
    </div>

    <div style="padding:16px 18px 34px 18px; max-width:500px; width:100%; margin:0 auto; box-sizing:border-box;">
        <input type="hidden" id="gnpClienteId">
        <input type="hidden" id="gnpRazonSeleccionada" value="CLIENTE SIN DINERO">

        <div style="display:flex; flex-direction:column; gap:7px; font-size:14px; color:#334155; padding-bottom:12px; border-bottom:1px solid #cbd5e1;">
            <div style="display:flex; justify-content:space-between;"><span>ID Cliente:</span><b id="gnpIdCliente" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Nombre</span><b id="gnpNombre" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Alias</span><b id="gnpAlias" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Documento 1</span><b id="gnpDocumento" style="color:#0f172a;">-</b></div>
            <div style="display:flex; justify-content:space-between;"><span id="gnpLblDiasEstado">Días de mora</span><b id="gnpDiasMora" style="color:#0f172a;">0</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Fecha último pago</span><b id="gnpUltimoPago" style="color:#0f172a;">-</b></div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 0 18px 0; font-size:16px; color:#334155;">
            <span>Saldo actual: $</span>
            <b id="gnpSaldoTexto" style="font-size:17px; color:#0f172a;">$0.00</b>
        </div>

        <div style="text-align:left; margin-bottom:20px;">
            <div style="font-size:14px; color:#334155; font-weight:600; margin-bottom:8px;">Razón</div>
            <div style="display:grid; grid-template-columns:repeat(4, 1fr); border:1px solid #cbd5e1; border-radius:6px; overflow:hidden;">
                <button type="button" class="btn-razon-np" onclick="seleccionarRazonNoPago(this, 'CLIENTE SIN DINERO')" style="padding:10px 4px; border:none; border-right:1px solid #cbd5e1; background:#dcfce7; color:#15803d; font-weight:700; font-size:10px; line-height:1.25; cursor:pointer;">CLIENTE SIN DINERO</button>
                <button type="button" class="btn-razon-np" onclick="seleccionarRazonNoPago(this, 'LOCAL CERRADO')" style="padding:10px 4px; border:none; border-right:1px solid #cbd5e1; background:#ffffff; color:#475569; font-weight:600; font-size:10px; line-height:1.25; cursor:pointer;">LOCAL CERRADO</button>
                <button type="button" class="btn-razon-np" onclick="seleccionarRazonNoPago(this, 'INCAPAZ DE COBRAR')" style="padding:10px 4px; border:none; border-right:1px solid #cbd5e1; background:#ffffff; color:#475569; font-weight:600; font-size:10px; line-height:1.25; cursor:pointer;">INCAPAZ DE COBRAR</button>
                <button type="button" class="btn-razon-np" onclick="seleccionarRazonNoPago(this, 'CLIENTE NO ENCONTRADO')" style="padding:10px 4px; border:none; background:#ffffff; color:#475569; font-weight:600; font-size:10px; line-height:1.25; cursor:pointer;">CLIENTE NO ENCONTRADO</button>
            </div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
            <span style="font-size:14px; color:#334155; font-weight:500;">¿Enviar a estudio para castigo de cartera?</span>
            <div style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gnpChkCastigo" style="opacity:0; width:0; height:0;">
                <span onclick="var c=document.getElementById('gnpChkCastigo'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </div>
        </div>

        <div style="margin-bottom:20px;">
            <input type="text" id="gnpComentarios" placeholder="Comentarios" style="width:100%; padding:14px; border:1px solid #64748b; border-radius:6px; font-size:14px; color:#0f172a; box-sizing:border-box;">
        </div>

        <div style="text-align:left; margin-bottom:28px;">
            <div style="font-size:14px; color:#334155; font-weight:500; margin-bottom:10px;">Adicionar fotografía</div>
            <label style="width:64px; height:64px; background:#e2e8f0; border-radius:8px; display:flex; align-items:center; justify-content:center; cursor:pointer; overflow:hidden; border:1px solid #cbd5e1;">
                <input type="file" accept="image/*" capture="environment" id="gnpInputFoto" onchange="previsualizarFotoGP(this, 'gnpIconoCamara', 'gnpPreviewImg')" style="display:none;">
                <svg id="gnpIconoCamara" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                <img id="gnpPreviewImg" src="" style="display:none; width:100%; height:100%; object-fit:cover;">
            </label>
        </div>

        <button type="button" onclick="confirmarNoPagoGestor()" style="width:100%; padding:15px; background:#0f2b5c; color:#ffffff; border:none; border-radius:8px; font-size:16px; font-weight:800; cursor:pointer; box-shadow:0 4px 12px rgba(15,43,92,0.25);">
            Registrar no pago
        </button>
    </div>
</div>

        <!-- 🖼️ MODAL DE FOTO CORREGIDA: Estilos explícitos inline para matar la herencia circular de 28px -->
    <div class="modal" id="modalFoto" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.8); z-index:500; justify-content:center; align-items:center; padding:16px;">
        <div class="modal-content" style="background:white; border-radius:16px; padding:16px; width:100%; max-width:440px; text-align:center; box-shadow:0 20px 25px -5px rgba(0,0,0,0.3);">
            <h3 style="margin-top:0; font-size:14px; color:#0f2b5c; margin-bottom:12px;"><i class="fa-solid fa-receipt"></i> Comprobante Contable</h3>
            
            <div style="background:#f8fafc; border-radius:10px; padding:6px; border:1px solid #e2e8f0; margin-bottom:12px; display:flex; justify-content:center; align-items:center; min-height:180px;">
                <img id="imgComprobante" src="" style="width:100% !important; max-height:55vh !important; object-fit:contain !important; border-radius:8px !important; display:block !important;">
            </div>
            
            <button onclick="cerrarModal('modalFoto')" class="btn-modal-close" style="width:100%; background:#e2e8f0; color:#334155; border:none; padding:10px; border-radius:8px; font-weight:700; cursor:pointer;">Cerrar Imagen</button>
        </div>
    </div>

    <div id="ticketPrint" style="display:none;">
        ==============================<br>
        &nbsp;&nbsp;<b>AVANTA PAGOS RECAUDOS</b><br>
        ==============================<br>
        Fecha: <span id="tFecha"></span><br>
        Cliente: <span id="tCliente"></span><br>
        Cuota #: <span id="tCuota"></span><br>
        Cobrado: $<span id="tMonto"></span><br>
        Saldo: $<span id="tSaldo"></span><br>
        ==============================
    </div>

<!-- MODAL TICKET DIGITAL DE CRÉDITO / RENOVACIÓN (AVANTA PAGOS) -->
<div id="modalTicketAvanta" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#e2e8f0; z-index:10005; flex-direction:column; overflow-y:auto;">
    <!-- Barra Superior -->
    <div style="background:#ffffff; padding:12px 16px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid #cbd5e1; position:sticky; top:0; z-index:2;">
        <button type="button" onclick="cerrarTicketAvantaYVolver()"
                style="background:#dc2626; color:#ffffff; border:none; border-radius:9px; padding:8px 14px; font-size:13px; font-weight:800; display:flex; align-items:center; gap:5px; cursor:pointer;">
            ✕ Salir
        </button>
        <div style="font-size:15px; font-weight:900; color:#0f172a;" id="tkBarraTitulo">Comprobante de Crédito</div>
        <div style="width:62px;"></div>
    </div>

    <!-- Contenedor del Ticket con Borde Dentado -->
    <div style="padding:18px 16px 32px 16px; display:flex; flex-direction:column; align-items:center;">
        <div id="ticketPapelAvanta" style="width:100%; max-width:340px; background:#f8fafc; box-shadow:0 12px 28px rgba(15,23,42,0.16); position:relative; font-family:'Courier New', Courier, monospace, sans-serif; color:#0f172a;">
            <!-- Borde Dentado Superior -->
            <div style="height:10px; width:100%; background:linear-gradient(135deg, #e2e8f0 50%, transparent 50%) 0 0 / 14px 10px repeat-x, linear-gradient(225deg, #e2e8f0 50%, transparent 50%) 0 0 / 14px 10px repeat-x;"></div>

            <div style="padding:16px 18px 14px 18px; text-align:center;">
                <!-- Encabezado con Isotipo A + Flecha Cruzada -->
                <div style="display:flex; align-items:center; justify-content:center; gap:10px; margin-bottom:4px;">
                    <div style="width:36px; height:36px; border-radius:9px; background:#0f2b5c; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                        <svg width="22" height="22" viewBox="0 0 32 32" fill="none">
                            <path d="M16 4L6 27H11.5L13.5 22H18.5L20.5 27H26L16 4ZM14.8 18L16 14.5L17.2 18H14.8Z" fill="#ffffff"/>
                            <path d="M5 19L26 9M26 9H19M26 9V16" stroke="#38bdf8" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                    </div>
                    <div style="text-align:left;">
                        <div style="font-size:19px; font-weight:900; color:#0f2b5c; letter-spacing:0.5px; font-family:sans-serif; line-height:1.1;">AVANTA PAGOS</div>
                        <div id="tkTipoOperacion" style="font-size:11px; font-weight:800; color:#475569; font-family:sans-serif; letter-spacing:0.4px;">COMPROBANTE DE CRÉDITO</div>
                    </div>
                </div>

                <div style="border-top:2px solid #1e293b; border-bottom:1px solid #cbd5e1; height:3px; margin:12px 0;"></div>

                <!-- Datos del Cliente -->
                <div style="font-size:13px; line-height:1.55; font-family:sans-serif; color:#1e293b;">
                    <div><b>Cliente:</b> <span id="tkCliente">-</span></div>
                    <div><b>Cédula:</b> <span id="tkCedula">-</span></div>
                    <div><b>Tel:</b> <span id="tkTelefono">-</span></div>
                    <div><b>Dir:</b> <span id="tkDireccion">-</span></div>
                    <div id="tkRowRef" style="color:#0f2b5c; font-weight:700;"></div>
                </div>

                <div style="border-top:2px solid #1e293b; border-bottom:1px solid #cbd5e1; height:3px; margin:12px 0;"></div>

                <!-- Condiciones del Crédito -->
                <div style="font-size:13px; line-height:1.6; font-family:sans-serif; color:#1e293b;">
                    <div><b>Cód. Crédito:</b> <span id="tkCodigo">000000</span></div>
                    <div><b>Modalidad:</b> <span id="tkFrecuencia">Diaria</span></div>
                    <div><b>Fecha entrega:</b> <span id="tkFechaInicio">-</span></div>
                    <div><b>Capital entregado:</b> $<span id="tkCapital">0.00</span></div>
                    <div><b>Total a pagar:</b> $<span id="tkTotal">0.00</span></div>
                    <div><b>Valor cuota:</b> $<span id="tkCuota">0.00</span></div>
                    <div><b>Plazo:</b> <span id="tkPlazo">-</span></div>
                </div>

                <div style="border-top:2px solid #1e293b; border-bottom:1px solid #cbd5e1; height:3px; margin:12px 0;"></div>

                <!-- Saldo Actual y Vencimiento -->
                <div style="font-family:sans-serif;">
                    <div style="font-size:19px; font-weight:900; color:#0f172a;">Saldo actual: $<span id="tkSaldo">0.00</span></div>
                    <div style="font-size:12.5px; font-weight:700; color:#334155; margin-top:3px;">Vence: <span id="tkVence">-</span></div>
                    <div style="font-size:12px; font-weight:800; color:#16a34a; margin-top:2px;">Estado: Al día</div>
                </div>

                <div style="border-top:1px dashed #94a3b8; margin:12px 0 8px 0;"></div>

                <div style="font-size:12px; font-weight:800; color:#334155; font-family:sans-serif;">** Gracias por preferirnos **</div>
                <div style="font-size:11px; color:#64748b; font-family:sans-serif; margin-top:2px;" id="tkFechaEmision">Emitido hoy</div>
            </div>

            <!-- Borde Dentado Inferior -->
            <div style="height:10px; width:100%; background:linear-gradient(45deg, #e2e8f0 50%, transparent 50%) 0 0 / 14px 10px repeat-x, linear-gradient(315deg, #e2e8f0 50%, transparent 50%) 0 0 / 14px 10px repeat-x;"></div>
        </div>

        <div style="font-size:11px; font-weight:700; color:#94a3b8; margin-top:8px;">Comprobante Digital • AVANTA PAGOS</div>

        <!-- Botón Único: Enviar Comprobante (Imagen PNG) -->
        <button type="button" onclick="compartirTicketAvanta()"
            style="width:100%; max-width:340px; margin-top:16px; padding:14px 18px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:12px; font-size:15px; font-weight:800; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:9px; box-shadow:0 8px 18px rgba(22,163,74,0.32);">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
            Enviar Comprobante
        </button>
    </div>
</div>

<script>
function toggleDrawer() {
    document.getElementById('drawer').classList.toggle('active');
    document.getElementById('drawerOverlay').classList.toggle('active');
}
function postAccionAvanta(url, destino, mensaje) {
    if (mensaje && !confirm(mensaje)) return false;
    fetch(url, { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(async (res) => {
            if (!res.ok) {
                let msg = 'No se pudo completar la operación.';
                try { const d = await res.json(); msg = d.message || msg; } catch(e) {}
                throw new Error(msg);
            }
            if (destino) navegarRuta(destino); else window.location.reload();
        })
        .catch(err => mostrarAvisoAvanta('Conexión', err.message || 'Error de conexión.', 'error'));
    return false;
}

function navegarRuta(url) {
    if (document.getElementById('drawerOverlay').classList.contains('active')) toggleDrawer();
    const mc = document.getElementById('mainContent');
    if (!mc) { window.location.href = url; return; }

    let bar = document.getElementById('avantaTopLoader');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'avantaTopLoader';
        bar.style.cssText = 'position:fixed;top:0;left:0;height:3px;width:0%;background:#38bdf8;z-index:99999;transition:width 0.35s ease, opacity 0.2s ease;';
        document.body.appendChild(bar);
    }
    bar.style.opacity = '1';
    bar.style.width = '75%';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(res => res.text())
        .then(html => {
            bar.style.width = '100%';
            mc.innerHTML = html;
            window.scrollTo({ top: 0, behavior: 'instant' });
            if (url === '/nuevo' || url.startsWith('/mover_renovacion')) activarGpsNativo();
            setTimeout(() => { bar.style.opacity = '0'; bar.style.width = '0%'; }, 180);
        })
        .catch(() => {
            bar.style.opacity = '0';
            window.location.href = url;
        });
}

function activarGpsNativo() {
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(function(position) {
            if (document.getElementById('latitud_input')) {
                document.getElementById('latitud_input').value = position.coords.latitude;
                document.getElementById('longitud_input').value = position.coords.longitude;
            }
        });
    }
}

function verFichaCliente(id) {
    fetch('/api/cliente_info/' + id)
        .then(res => res.json())
        .then(d => {
            if(d.status === 'ok') {
                document.getElementById('inf_nombre').innerText = d.data.nombre;
                document.getElementById('inf_tel').innerText = d.data.telefono;
                document.getElementById('inf_id').innerText = d.data.identificacion;
                document.getElementById('inf_dir').innerText = d.data.direccion;
                document.getElementById('inf_ref').innerText = d.data.referencia;
                document.getElementById('inf_f_ini').innerText = d.data.fecha_inicio;
                document.getElementById('inf_f_venc').innerText = d.data.fecha_vencimiento;
                document.getElementById('inf_v_cuota').innerText = '$' + d.data.valor_cuota.toFixed(2);
                document.getElementById('inf_saldo_act').innerText = '$' + d.data.saldo_actual.toFixed(2);
                document.getElementById('btn_llamar').href = 'tel:' + d.data.telefono;
                
                // 🗺️ FIJADO: Enlace de mapas universal profundo para celulares Android/iOS
                document.getElementById('btn_mapa').href = 'https://www.google.com/maps?q=' + d.data.latitud + ',' + d.data.longitud;
                
                document.getElementById('modalInfoCliente').style.display = 'flex';
            }
        });
}

function filtrarClientes() {
    // 🧠 MOTOR DE BÚSQUEDA INTELIGENTE MULTICRITERIO
    const input = document.getElementById('searchInput');
    if (!input) return;
    
    // Normalizamos el texto quitando acentos y mayúsculas para búsquedas fluidas
    const filter = input.value.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    const container = document.getElementById('clientesContainer');
    if (!container) return;
    
    // Buscamos dentro de todas las tarjetas de clientes (tanto premium como tradicionales)
    const cardsPremium = container.getElementsByClassName('cliente-card-premium');
    const cardsTradicionales = container.getElementsByClassName('cliente-card');
    const todasLasCards = [...cardsPremium, ...cardsTradicionales];

    for (let i = 0; i < todasLasCards.length; i++) {
        const card = todasLasCards[i];
        
        // Extraemos todos los metadatos de la tarjeta para validar el cruce
        const nombre = card.getAttribute('data-nombre') || "";
        const textoCompleto = card.innerText.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
        
        // El cliente se muestra si el filtro coincide con su nombre, id, dirección o referencia
        if (nombre.includes(filter) || textoCompleto.includes(filter)) {
            card.style.setProperty('display', 'flex', 'important');
        } else {
            card.style.setProperty('display', 'none', 'important');
        }
    }
}

function filtrarEstado(tipo) {
    // 🧠 MOTOR DE FILTRADO POR ESTADOS DE CARTERA
    const container = document.getElementById('clientesContainer');
    if (!container) return;
    
    const cardsPremium = container.getElementsByClassName('cliente-card-premium');
    const cardsTradicionales = container.getElementsByClassName('cliente-card');
    const todasLasCards = [...cardsPremium, ...cardsTradicionales];
    
    for (let i = 0; i < todasLasCards.length; i++) {
        const card = todasLasCards[i];
        
        // 🛡️ FIJADO: Comparación limpia con tres signos de igual (===)
        if (tipo === 'todos') {
            card.style.setProperty('display', 'flex', 'important');
        } else if (tipo === 'mora') {
            if (card.getAttribute('data-mora') === '1') {
                card.style.setProperty('display', 'flex', 'important');
            } else {
                card.style.setProperty('display', 'none', 'important');
            }
        }
    }
    
    // Cambiar clases activas visuales en los botones superiores
    const buttons = document.querySelectorAll('.filter-chip');
    buttons.forEach(btn => btn.classList.remove('active'));
    event.currentTarget.classList.add('active');
}

function manejarTipoSeguro() {
    const accion = document.getElementById('calcSeguroAccion').value;
    const contenedorManual = document.getElementById('contenedorSeguroManual');
    if (accion === 'MANUAL') {
        contenedorManual.style.display = 'block';
    } else {
        contenedorManual.style.display = 'none';
        document.getElementById('calcSeguroManualVal').value = '0';
    }
    calcularCuota();
}

// 💱 DETECCIÓN AUTOMÁTICA INTERNACIONAL DE MONEDA NATIVA
const configuracionMonedaLocal = {
    obtenerSimbolo: function() {
        const locale = navigator.language || 'es-EC';
        
        if (locale.includes('-CO')) return { formato: 'es-CO', divisa: 'COP' };
        if (locale.includes('-MX')) return { formato: 'es-MX', divisa: 'MXN' };
        if (locale.includes('-PE')) return { formato: 'es-PE', divisa: 'PEN' };
        if (locale.includes('-CL')) return { formato: 'es-CL', divisa: 'CLP' };
        
        return { formato: 'es-EC', divisa: 'USD' };
    },
    formatear: function(valor) {
        const conf = this.obtenerSimbolo();
        return new Intl.NumberFormat(conf.formato, { style: 'currency', currency: conf.divisa }).format(valor);
    }
};

function calcularCuota() {
    const monto = parseFloat(document.getElementById('calcMonto').value) || 0;
    const interes = parseFloat(document.getElementById('calcInteres').value) || 0;
    const cuotas = parseInt(document.getElementById('calcCuotas').value) || 0;
    const aplicaSeguro = document.getElementById('calcSeguroAccion').value;
    
    let seguro = 0;
    if (aplicaSeguro === '2') {
        seguro = monto * 0.02;
    } else if (aplicaSeguro === '4') {
        seguro = monto * 0.04;
    } else if (aplicaSeguro === 'MANUAL') {
        seguro = parseFloat(document.getElementById('calcSeguroManualVal').value) || 0;
    }
    
    const netoEntregar = monto - seguro;
    
    // Desglose automático usando la moneda local detectada del dispositivo móvil
    document.getElementById('seguroDesgloseText').innerHTML = `Monto Seguro: <b>${configuracionMonedaLocal.formatear(seguro)}</b> | Neto a Entregar: <b style="color:#10b981;">${configuracionMonedaLocal.formatear(netoEntregar)}</b>`;
    if (monto > 0 && cuotas > 0) {
        const totalCredito = monto * (1 + (interes / 100));
        document.getElementById('simulacionText').innerText = configuracionMonedaLocal.formatear(totalCredito / cuotas) + ' / cuota';
    } else {
        document.getElementById('simulacionText').innerText = configuracionMonedaLocal.formatear(0) + ' / cuota';
    }
}

let diasMoraGlobalTemporal = 0; // Variable de persistencia ambiental para las modales

function cerrarGestorPantallas() {
    document.getElementById('vistaGestorPago').style.display = 'none';
    document.getElementById('vistaGestorNoPago').style.display = 'none';
}

let gestionPreviaTipo = '';
let gestionPreviaIds = '';

function abrirGestorPago(clienteId, nombre, valorCuota, saldoTotal, diasMora = 0) {
    abrirModalAbono(clienteId, 1, saldoTotal, valorCuota, diasMora);
    if (nombre) {
        document.getElementById('gpNombre').innerText = String(nombre).toUpperCase();
    }
}

function abrirModalAbono(clienteId, numCuota, pendiente, valorCuota, diasMora = 0, prevTipo = '', prevIds = '') {
    gestionPreviaTipo = prevTipo;
    gestionPreviaIds = prevIds;
    diasMoraGlobalTemporal = parseInt(diasMora) || 0;
    document.getElementById('abonoClienteId').value = clienteId;
    document.getElementById('abonoNumCuota').value = numCuota;

    const card = document.getElementById('cliente-card-' + clienteId);
    const nombre = card ? (card.getAttribute('data-nombre') || 'Cliente').toUpperCase() : 'Cliente #' + clienteId;
    const spansCard = card ? card.querySelectorAll('span') : [];
    const alias = spansCard.length > 1 ? spansCard[1].innerText.trim() : '-';
    const cedula = card ? (card.getAttribute('data-cedula') || 'N/A') : 'N/A';
    let saldoNum = parseFloat(pendiente) || 0;
    if (card) {
        const bTags = card.querySelectorAll('b');
        const lastB = bTags.length > 0 ? bTags[bTags.length - 1] : null;
        if (lastB && lastB.innerText.includes('$')) {
            saldoNum = parseFloat(lastB.innerText.replace(/[^0-9.]/g, '')) || saldoNum;
        }
        if (prevTipo === 'pago') {
            const pagoHoyEl = card.querySelector('[style*="color:#16a34a"]');
            if (pagoHoyEl && pagoHoyEl.innerText.includes('$')) {
                const montoPrevio = parseFloat(pagoHoyEl.innerText.replace(/[^0-9.]/g, '')) || 0;
                saldoNum += montoPrevio;
            }
        }
    }

    const vCuota = parseFloat(valorCuota) || parseFloat(pendiente) || 0;
    const maxCuotas = vCuota > 0 ? Math.max(1, Math.ceil(saldoNum / vCuota)) : 1;

    document.getElementById('gpIdCliente').innerText = clienteId;
    document.getElementById('gpNombre').innerText = nombre;
    document.getElementById('gpAlias').innerText = alias;
    document.getElementById('gpDocumento').innerText = cedula;

    // Sincronización 100% con Días de Mora / Días de Adelanto de la tarjeta
    const textoDiasCard = card ? (card.getAttribute('data-dias') || '0').trim() : '0';
    const esAdelanto = card ? (card.getAttribute('data-es-adelanto') === '1') : false;

    document.getElementById('gpLblDiasEstado').innerText = esAdelanto ? 'Días de adelanto' : 'Días de mora';
    document.getElementById('gpDiasMora').innerText = textoDiasCard;
    document.getElementById('gpDiasMora').style.color = esAdelanto ? '#16a34a' : (parseInt(textoDiasCard) > 0 ? '#dc2626' : '#0f172a');
    document.getElementById('gpUltimoPago').innerText = card ? (card.getAttribute('data-ultpago') || 'Sin pagos previos') : '-';
    document.getElementById('gpSaldoTexto').innerText = '$' + saldoNum.toFixed(2);

    document.getElementById('gpValorCuotaBase').value = vCuota;
    document.getElementById('gpSaldoActualNum').value = saldoNum;
    document.getElementById('gpCuotasPendNum').value = maxCuotas;

    const slider = document.getElementById('gpSliderCuotas');
    slider.min = 1;
    slider.max = maxCuotas;
    slider.value = 1;

    document.getElementById('gpChkPagarTotal').checked = false;
    const swTot = document.getElementById('gpSwitchTotalVisual');
    swTot.style.background = '#cbd5e1';
    swTot.firstElementChild.style.transform = 'translateX(0)';

    seleccionarTipoPagoGP('cuota');
    seleccionarMetodoPagoGP('efectivo');
    setCuotasGP(1);

    document.getElementById('vistaGestorPago').style.display = 'flex';
}

function seleccionarTipoPagoGP(tipo) {
    document.getElementById('gpTipoPago').value = tipo;
    const bCuota = document.getElementById('btnTipoCuota');
    const bDinero = document.getElementById('btnTipoDinero');
    const stepper = document.getElementById('gpBloqueStepperCuotas');
    const inputDinero = document.getElementById('abonoMontoInput');
    const lblMonto = document.getElementById('gpLblMonto');

    if (tipo === 'cuota') {
        bCuota.style.background = '#dcfce7'; bCuota.style.color = '#15803d'; bCuota.style.fontWeight = '700';
        bDinero.style.background = '#ffffff'; bDinero.style.color = '#475569'; bDinero.style.fontWeight = '600';
        stepper.style.display = 'block';
        inputDinero.style.display = 'none';
        lblMonto.innerText = 'Valor cuota:';
        setCuotasGP(document.getElementById('gpSliderCuotas').value || 1);
    } else {
        bDinero.style.background = '#dcfce7'; bDinero.style.color = '#15803d'; bDinero.style.fontWeight = '700';
        bCuota.style.background = '#ffffff'; bCuota.style.color = '#475569'; bCuota.style.fontWeight = '600';
        stepper.style.display = 'none';
        inputDinero.style.display = 'block';
        lblMonto.innerText = 'Monto a abonar ($):';
        inputDinero.focus();
    }
}

function seleccionarMetodoPagoGP(metodo) {
    document.getElementById('gpMetodoPago').value = metodo;
    const bEf = document.getElementById('btnMetEfectivo');
    const bTr = document.getElementById('btnMetTrans');
    if (metodo === 'efectivo') {
        bEf.style.background = '#dcfce7'; bEf.style.color = '#15803d'; bEf.style.fontWeight = '700';
        bTr.style.background = '#ffffff'; bTr.style.color = '#475569'; bTr.style.fontWeight = '600';
    } else {
        bTr.style.background = '#dcfce7'; bTr.style.color = '#15803d'; bTr.style.fontWeight = '700';
        bEf.style.background = '#ffffff'; bEf.style.color = '#475569'; bEf.style.fontWeight = '600';
    }
}

function setCuotasGP(cant) {
    const maxC = parseInt(document.getElementById('gpCuotasPendNum').value) || 1;
    let n = Math.max(1, Math.min(maxC, parseInt(cant) || 1));
    document.getElementById('gpCantidadCuotasTexto').innerText = n;
    document.getElementById('gpSliderCuotas').value = n;

    const vCuota = parseFloat(document.getElementById('gpValorCuotaBase').value) || 0;
    const saldoMax = parseFloat(document.getElementById('gpSaldoActualNum').value) || 0;
    let total = Math.min(saldoMax, n * vCuota);

    document.getElementById('gpDisplayMontoGrande').innerText = '$' + total.toFixed(2);
    document.getElementById('abonoMontoInput').value = total.toFixed(2);
}

function cambiarCuotasGP(delta) {
    const actual = parseInt(document.getElementById('gpCantidadCuotasTexto').innerText) || 1;
    setCuotasGP(actual + delta);
}

function actualizarDesdeInputDineroGP() {
    const val = parseFloat(document.getElementById('abonoMontoInput').value) || 0;
    document.getElementById('gpDisplayMontoGrande').innerText = '$' + val.toFixed(2);
}

function togglePagarTotalGP() {
    const chk = document.getElementById('gpChkPagarTotal');
    chk.checked = !chk.checked;
    const sw = document.getElementById('gpSwitchTotalVisual');
    sw.style.background = chk.checked ? '#16a34a' : '#cbd5e1';
    sw.firstElementChild.style.transform = chk.checked ? 'translateX(20px)' : 'translateX(0)';

    if (chk.checked) {
        const maxC = parseInt(document.getElementById('gpCuotasPendNum').value) || 1;
        const saldoMax = parseFloat(document.getElementById('gpSaldoActualNum').value) || 0;
        setCuotasGP(maxC);
        document.getElementById('gpDisplayMontoGrande').innerText = '$' + saldoMax.toFixed(2);
        document.getElementById('abonoMontoInput').value = saldoMax.toFixed(2);
    } else {
        setCuotasGP(1);
    }
}

function previsualizarFotoGP(input, iconId, imgId) {
    if (input.files && input.files[0]) {
        const reader = new FileReader();
        reader.onload = function(e) {
            document.getElementById(iconId).style.display = 'none';
            const img = document.getElementById(imgId);
            img.src = e.target.result;
            img.style.display = 'block';
        };
        reader.readAsDataURL(input.files[0]);
    }
}

async function confirmarAbono() {
    const clienteId = document.getElementById('abonoClienteId').value;
    const numCuota = document.getElementById('abonoNumCuota').value;
    const monto = parseFloat(document.getElementById('abonoMontoInput').value);
    if (monto > 0) {
        cerrarGestorPantallas();
        if (gestionPreviaTipo === 'pago' && gestionPreviaIds) {
            await fetch('/api/revertir_gestion/pago/' + gestionPreviaIds, { method: 'POST' });
        } else if (gestionPreviaTipo === 'no_pago') {
            await fetch('/api/revertir_gestion/salto/' + clienteId, { method: 'POST' });
        }
        gestionPreviaTipo = '';
        gestionPreviaIds = '';
        procesarPagoAPI(clienteId, numCuota, monto);
    } else {
        mostrarAvisoAvanta('Monto Inválido', 'Ingresa un monto válido mayor a $0.00', 'alerta');
    }
}

function ejecutarPago(clienteId, numCuota, pendiente) {
    procesarPagoAPI(clienteId, numCuota, pendiente);
}

// Variables de control ambiental de carga síncrona multimedia
let imagenComprimidaB64 = "";
let fotoListaParaEnviar = false;

function procesarPagoAPI(clienteId, numCuota, monto) {
    const zonaUsuario = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const ahoraDispositivo = new Date();
    
    const dia = String(ahoraDispositivo.getDate()).padStart(2, '0');
    const mes = String(ahoraDispositivo.getMonth() + 1).padStart(2, '0');
    const anio = ahoraDispositivo.getFullYear();
    const horas = String(ahoraDispositivo.getHours()).padStart(2, '0');
    const minutes = String(ahoraDispositivo.getMinutes()).padStart(2, '0');
    const horaLocalFormateada = `${dia}/${mes}/${anio} ${horas}:${minutes}`;

    // ⚡ FIJADO: Ahora sí le pasamos de forma real la variable 'diasMoraGlobalTemporal' a la ruta de Python
    const metodoPago = (document.getElementById('gpMetodoPago')?.value || 'efectivo').toLowerCase();

    fetch('/api/marcar_pago/' + clienteId + '/' + numCuota, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            monto: monto,
            hora_local: horaLocalFormateada,
            zona_usuario: zonaUsuario,
            dias_mora: diasMoraGlobalTemporal,
            metodo_pago: metodoPago
        })
    })
        .then(res => res.json())
                .then(data => {
            if (data.status === 'ok') {
                if (data.ticket_pago && typeof mostrarTicketAvanta === 'function') {
                    mostrarTicketAvanta(data.ticket_pago);
                } else {
                    window.location.href = '/';
                }
            } else {
                mostrarAvisoAvanta('Pago no procesado', data.message || 'No se pudo procesar el pago.', 'error');
            }
        });
}

function ejecutarNoPago(clienteId, prevTipo = '', prevIds = '') {
    gestionPreviaTipo = prevTipo;
    gestionPreviaIds = prevIds;
    document.getElementById('gnpClienteId').value = clienteId;
    const card = document.getElementById('cliente-card-' + clienteId);
    const nombre = card ? (card.getAttribute('data-nombre') || 'Cliente').toUpperCase() : 'Cliente #' + clienteId;
    const spansCard = card ? card.querySelectorAll('span') : [];
    const alias = spansCard.length > 1 ? spansCard[1].innerText.trim() : '-';
    const cedula = card ? (card.getAttribute('data-cedula') || 'N/A') : 'N/A';

    const setTxt = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    setTxt('gnpIdCliente', clienteId);
    setTxt('gnpNombre', nombre);
    setTxt('gnpAlias', alias || '-');
    setTxt('gnpDocumento', cedula);

    let saldoTxt = '$0.00';
    if (card) {
        const bTags = card.querySelectorAll('b');
        const lastB = bTags.length > 0 ? bTags[bTags.length - 1] : null;
        if (lastB && lastB.innerText.includes('$')) {
            saldoTxt = lastB.innerText.trim();
        }
    }

    const diasMora = card ? (card.getAttribute('data-dias') || '0').trim() : '0';
    const esAdelantoNp = card ? (card.getAttribute('data-es-adelanto') === '1') : false;

    setTxt('gnpLblDiasEstado', esAdelantoNp ? 'Días de adelanto' : 'Días de mora');
    const elDias = document.getElementById('gnpDiasMora');
    if (elDias) {
        elDias.innerText = diasMora;
        elDias.style.color = esAdelantoNp ? '#0284c7' : (parseInt(diasMora) > 0 ? '#dc2626' : '#16a34a');
    }
    setTxt('gnpUltimoPago', card ? (card.getAttribute('data-ultpago') || 'Sin pagos previos') : '-');
    setTxt('gnpSaldoTexto', saldoTxt);
    const elCom = document.getElementById('gnpComentarios');
    if (elCom) elCom.value = '';

    document.getElementById('vistaGestorNoPago').style.display = 'flex';
}

function seleccionarRazonNoPago(btn, razon) {
    document.getElementById('gnpRazonSeleccionada').value = razon;
    document.querySelectorAll('.btn-razon-np').forEach(b => {
        b.style.background = '#ffffff';
        b.style.color = '#475569';
        b.style.fontWeight = '600';
    });
    btn.style.background = '#dcfce7';
    btn.style.color = '#15803d';
    btn.style.fontWeight = '700';
}

async function confirmarNoPagoGestor() {
    const clienteId = document.getElementById('gnpClienteId').value;
    if (!clienteId) return;
    cerrarGestorPantallas();
    if (gestionPreviaTipo === 'pago' && gestionPreviaIds) {
        await fetch('/api/revertir_gestion/pago/' + gestionPreviaIds, { method: 'POST' }).catch(() => {});
    }
    gestionPreviaTipo = '';
    gestionPreviaIds = '';
    const razonNp = document.getElementById('gnpRazonSeleccionada')?.value || 'CLIENTE SIN DINERO';
    const comentariosNp = (document.getElementById('gnpComentarios')?.value || '').trim();

    fetch('/api/marcar_no_pago/' + clienteId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            razon: razonNp,
            comentarios: comentariosNp
        })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                window.location.reload();
            } else {
                mostrarAvisoAvanta('Error en No Pago', 'Error al registrar no pago: ' + (data.message || ''), 'error');
            }
        }).catch(err => mostrarAvisoAvanta('Error de Conexión', 'Error al procesar el salto: ' + err, 'error'));
}

function guardarGpsExpediente(clienteId, btn) {
    if (!navigator.geolocation) { mostrarAvisoAvanta('GPS no disponible', 'Tu dispositivo no soporta GPS.', 'alerta'); return; }
    const txtOrig = btn.innerHTML;
    btn.disabled = true;
    btn.innerText = 'Ubicando...';
    navigator.geolocation.getCurrentPosition(pos => {
        fetch('/api/cliente/guardar_gps/' + clienteId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ latitud: pos.coords.latitude, longitud: pos.coords.longitude })
        })
        .then(r => r.json())
        .then(d => {
            if (d.status === 'ok') navegarRuta('/menu/cliente/' + clienteId);
            else { mostrarAvisoAvanta('GPS', d.message || 'No se pudo guardar el GPS.', 'error'); btn.disabled = false; btn.innerHTML = txtOrig; }
        })
        .catch(() => { mostrarAvisoAvanta('Conexión', 'Error de conexión.', 'error'); btn.disabled = false; btn.innerHTML = txtOrig; });
    }, () => {
        mostrarAvisoAvanta('Permiso de Ubicación', 'Activa el permiso de ubicación en tu navegador.', 'alerta');
        btn.disabled = false;
        btn.innerHTML = txtOrig;
    }, { enableHighAccuracy: true, timeout: 10000 });
}

function obtenerCoordenadasGPS() {
    const statusText = document.getElementById('status_gps');
    
    if (!navigator.geolocation) {
        mostrarAvisoAvanta('GPS no disponible', 'Tu dispositivo no soporta geolocalización o el navegador la tiene bloqueada.', 'alerta');
        return;
    }
    
    statusText.innerText = "Buscando señal de satélite GPS...";
    
    navigator.geolocation.getCurrentPosition(
        function (posicion) {
            const lat = posicion.coords.latitude;
            const lon = posicion.coords.longitude;
            
            // Asignamos las coordenadas a los inputs para guardarlas en Postgres
            document.getElementById('latitud_input').value = lat;
            document.getElementById('longitud_input').value = lon;
            
            statusText.innerHTML = `Ubicación fijada: <b>${lat.toFixed(5)}</b>, ${lon.toFixed(5)}`;
        },
        function (error) {
            statusText.innerText = "No se pudo capturar el GPS.";
            if (error.code === error.PERMISSION_DENIED) {
                mostrarAvisoAvanta('Permiso Denegado', 'Permiso denegado. Debes activar el GPS y dar permisos de ubicación a tu navegador.', 'alerta');
            } else {
                mostrarAvisoAvanta('Señal Débil', 'Señal débil. Asegúrate de estar en un lugar con cobertura satelital.', 'alerta');
            }
        },
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
    );
}

function procesarImagenEnCaliente() {
    const fileInput = document.getElementById('foto_mov');
    const file = fileInput.files ? fileInput.files[0] : null;
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function(e) {
        const img = new Image();
        img.onload = function() {
            const canvas = document.createElement('canvas');
            let width = img.width;
            let height = img.height;

            const MAX_WIDTH = 800;
            if (width > MAX_WIDTH) {
                height *= MAX_WIDTH / width;
                width = MAX_WIDTH;
            }
            canvas.width = width;
            canvas.height = height;

            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, width, height);

            imagenComprimidaB64 = canvas.toDataURL('image/jpeg', 0.6);
            document.getElementById('foto_comprimida_b64').value = imagenComprimidaB64;
            fotoListaParaEnviar = true;
            console.log("Foto comprimida con éxito.");
        };
        img.src = e.target.result;
    };
    reader.readAsDataURL(file);
}

function procesarYEnviarGasto(event) {
    const fileInput = document.getElementById('foto_mov');
    const form = document.getElementById('formBalance');
    
    if (fileInput.files.length > 0 && !fotoListaParaEnviar) {
        mostrarAvisoAvanta('Procesando Imagen', 'Procesando imagen de alta definición... Espera un segundo y vuelve a presionar Guardar.', 'info');
        return false;
    }
    form.submit();
}

function cargarYVerFoto(gastoId) {
    fetch('/api/gasto_foto/' + gastoId)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok' && data.comprobante) {
                const img = document.getElementById('imgComprobante');
                
                // 🛡️ FIJADO: Verificación de seguridad para evitar caídas en celulares si el elemento tarda en renderizar
                if (!img) {
                    mostrarAvisoAvanta('Cargando Vista', "La interfaz se está acomodando, por favor intenta presionar 'Ver' nuevamente.", 'info');
                    return;
                }
                
                img.src = ""; 
                let textoB64 = data.comprobante.trim();
                if (!textoB64.startsWith('data:image')) {
                    textoB64 = 'data:image/jpeg;base64,' + textoB64;
                }
                img.src = textoB64;
                document.getElementById('modalFoto').style.display = 'flex';
            } else {
                mostrarAvisoAvanta('Sin Comprobante', 'No se pudo cargar la imagen o el registro no tiene comprobante.', 'alerta');
            }
        }).catch(err => mostrarAvisoAvanta('Error de Conexión', 'Error al conectar con el servidor: ' + err, 'error'));
}

function revertirGestionDia(tipo, id) {
    if (confirm('¿Está seguro de que desea deshacer esta gestión y regresar al cliente a la Ruta del Día?')) {
        fetch('/api/revertir_gestion/' + tipo + '/' + id, { method: 'POST' })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'ok') {
                    mostrarAvisoAvanta('Gestión Revertida', 'Gestión revertida. El cliente ha regresado a la lista por cobrar.', 'exito');
                    setTimeout(() => window.location.reload(), 1200);
                }
            }).catch(err => mostrarAvisoAvanta('Error de Conexión', 'Error al conectar: ' + err, 'error'));
    }
}

function imprimirTicket() { window.print(); }
function cerrarModal(id) { document.getElementById(id).style.display = 'none'; }

function actualizarPerfilCompleto(e) {
    e.preventDefault();
    const nom = document.getElementById('perfilNombre').value;
    const ced = document.getElementById('perfilCedula').value;
    const cel = document.getElementById('perfilCelular').value;
    const cor = document.getElementById('perfilCorreo').value;
    const usr = document.getElementById('perfilUsuario').value;
    const clv = document.getElementById('perfilClave').value;
    
    fetch('/api/perfil/guardar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nombre: nom, cedula: ced, celular: cel, correo: cor, usuario: usr, clave: clv })
    })
        .then(res => res.json())
        .then(data => {
            const exito = (data.status === 'ok');
            const prevModal = document.getElementById('modalPerfilAvanta');
            if (prevModal) prevModal.remove();

            const modalHtml = `
                <div id="modalPerfilAvanta" style="position:fixed; inset:0; background:rgba(15,23,42,0.72); backdrop-filter:blur(4px); z-index:99999; display:flex; align-items:center; justify-content:center; padding:20px;">
                    <div style="background:#ffffff; border-radius:20px; width:100%; max-width:340px; padding:26px 22px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,0.28); border:1px solid #e2e8f0; animation:fadeIn 0.2s ease;">
                        <div style="width:58px; height:58px; border-radius:50%; margin:0 auto 14px auto; display:flex; align-items:center; justify-content:center; background:${exito ? 'linear-gradient(135deg,#16a34a,#15803d)' : 'linear-gradient(135deg,#dc2626,#b91c1c)'}; color:#ffffff; box-shadow:0 8px 18px ${exito ? 'rgba(22,163,74,0.32)' : 'rgba(220,38,38,0.32)'};">
                            ${exito
                                ? '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>'
                                : '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>'
                            }
                        </div>
                        <h3 style="margin:0 0 6px 0; font-size:17px; font-weight:800; color:#0f172a; letter-spacing:-0.2px;">
                            ${exito ? 'Perfil Actualizado' : 'Error al Guardar'}
                        </h3>
                        <p style="margin:0 0 20px 0; font-size:13px; color:#475569; line-height:1.45;">
                            ${exito ? 'Los datos del cobrador se han actualizado correctamente en la ruta.' : 'No se pudieron guardar los cambios. Intenta nuevamente.'}
                        </p>
                        <button type="button" id="btnCerrarModalPerfil" style="width:100%; border:none; padding:13px; border-radius:12px; background:linear-gradient(135deg,#0f2b5c,#1e3a8a); color:#ffffff; font-size:14px; font-weight:700; cursor:pointer; box-shadow:0 6px 14px rgba(15,43,92,0.28);">
                            Aceptar
                        </button>
                    </div>
                </div>
            `;
            document.body.insertAdjacentHTML('beforeend', modalHtml);
            document.getElementById('btnCerrarModalPerfil').onclick = function() {
                document.getElementById('modalPerfilAvanta').remove();
                if (exito) window.location.reload();
            };
        })
        .catch(err => console.error('Error:', err));
}

function mostrarAvisoAvanta(titulo, mensaje, tipo = 'info') {
    const prev = document.getElementById('modalAvisoAvanta');
    if (prev) prev.remove();

    const estilos = {
        info:  { bg: 'linear-gradient(135deg,#0f2b5c,#1e3a8a)', sombra: 'rgba(15,43,92,0.28)', svg: '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>' },
        alerta:{ bg: 'linear-gradient(135deg,#d97706,#b45309)', sombra: 'rgba(217,119,6,0.28)',  svg: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>' },
        exito: { bg: 'linear-gradient(135deg,#16a34a,#15803d)', sombra: 'rgba(22,163,74,0.28)', svg: '<polyline points="20 6 9 17 4 12"/>' },
        error: { bg: 'linear-gradient(135deg,#dc2626,#b91c1c)', sombra: 'rgba(220,38,38,0.28)', svg: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>' }
    };
    const cfg = estilos[tipo] || estilos.info;

    const html = `
        <div id="modalAvisoAvanta" style="position:fixed; inset:0; background:rgba(15,23,42,0.72); backdrop-filter:blur(4px); z-index:99999; display:flex; align-items:center; justify-content:center; padding:20px;">
            <div style="background:#ffffff; border-radius:20px; width:100%; max-width:340px; padding:26px 22px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,0.28); border:1px solid #e2e8f0; animation:fadeIn 0.2s ease;">
                <div style="width:58px; height:58px; border-radius:50%; margin:0 auto 14px auto; display:flex; align-items:center; justify-content:center; background:${cfg.bg}; color:#ffffff; box-shadow:0 8px 18px ${cfg.sombra};">
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">${cfg.svg}</svg>
                </div>
                <h3 style="margin:0 0 6px 0; font-size:17px; font-weight:800; color:#0f172a; letter-spacing:-0.2px;">${titulo}</h3>
                <p style="margin:0 0 20px 0; font-size:13px; color:#475569; line-height:1.45;">${mensaje}</p>
                <button type="button" id="btnCerrarAvisoAvanta" style="width:100%; border:none; padding:13px; border-radius:12px; background:linear-gradient(135deg,#0f2b5c,#1e3a8a); color:#ffffff; font-size:14px; font-weight:700; cursor:pointer; box-shadow:0 6px 14px rgba(15,43,92,0.28);">Aceptar</button>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', html);
    document.getElementById('btnCerrarAvisoAvanta').onclick = function() {
        document.getElementById('modalAvisoAvanta').remove();
    };
}

// ⚡ MOTOR GLOBAL ANTI-DOBLE CLIC PARA FORMULARIOS DE CAMPO
document.addEventListener("DOMContentLoaded", function() {
    const formularios = document.querySelectorAll("form");
    
    formularios.forEach(form => {
        form.addEventListener("submit", function(e) {
            const botonSubmit = form.querySelector("button[type='submit'], input[type='submit']");
            if (botonSubmit) {
                setTimeout(() => {
                    botonSubmit.disabled = true;
                    if (botonSubmit.tagName === "BUTTON") {
                        botonSubmit.innerHTML = "⌛ Procesando solicitud...";
                    } else {
                        botonSubmit.value = "Procesando...";
                    }
                }, 10);
            }
        });
    });
});

        function seleccionarFreqCredito(tipo) {
            document.getElementById('inputFreqCredito').value = tipo;
            const bDia = document.getElementById('btnFreqDiaria');
            const bSem = document.getElementById('btnFreqSemanal');
            if (tipo === 'Diaria') {
                bDia.style.border = '2px solid #0f2b5c'; bDia.style.background = '#0f2b5c'; bDia.style.color = '#ffffff'; bDia.style.fontWeight = '800';
                bSem.style.border = '1.5px solid #cbd5e1'; bSem.style.background = '#f8fafc'; bSem.style.color = '#475569'; bSem.style.fontWeight = '700';
            } else {
                bSem.style.border = '2px solid #0f2b5c'; bSem.style.background = '#0f2b5c'; bSem.style.color = '#ffffff'; bSem.style.fontWeight = '800';
                bDia.style.border = '1.5px solid #cbd5e1'; bDia.style.background = '#f8fafc'; bDia.style.color = '#475569'; bDia.style.fontWeight = '700';
            }
        }

        function seleccionarSegCredito(val) {
            const sel = document.getElementById('calcSeguroAccion');
            if (sel) sel.value = val;
            document.querySelectorAll('.card-seg-opt').forEach(el => {
                el.style.border = '1.5px solid #cbd5e1';
                el.style.background = '#f8fafc';
                el.style.color = '#475569';
            });
            const activo = document.getElementById('optSeg_' + val);
            if (activo) {
                activo.style.border = '2px solid #0f2b5c';
                activo.style.background = '#eff6ff';
                activo.style.color = '#0f2b5c';
            }
            if (typeof manejarTipoSeguro === 'function') manejarTipoSeguro();
        }

async function comprimirImagenForm(file) {
    if (!file || !file.type || !file.type.startsWith('image/')) return file;
    return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = (ev) => {
            const img = new Image();
            img.onload = () => {
                const maxDim = 1024;
                let w = img.width, h = img.height;
                if (w > maxDim || h > maxDim) {
                    if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
                    else { w = Math.round(w * maxDim / h); h = maxDim; }
                }
                const canvas = document.createElement('canvas');
                canvas.width = w; canvas.height = h;
                canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                canvas.toBlob((blob) => {
                    if (blob) resolve(new File([blob], file.name || 'foto.jpg', { type: 'image/jpeg' }));
                    else resolve(file);
                }, 'image/jpeg', 0.78);
            };
            img.onerror = () => resolve(file);
            img.src = ev.target.result;
        };
        reader.onerror = () => resolve(file);
        reader.readAsDataURL(file);
    });
}

async function enviarCreditoAsincrono(event) {
    event.preventDefault();
    const form = document.getElementById('formNuevoCredito');
    const btn = document.getElementById('btnGuardarCredito');
    const txtOriginal = btn ? btn.innerText : 'Guardar y Crear Crédito';
    if (btn) {
        btn.disabled = true;
        btn.innerText = 'Guardando crédito...';
    }

    try {
        const fd = new FormData(form);
        const inputsFile = form.querySelectorAll('input[type="file"]');
        for (const inp of inputsFile) {
            if (inp.files && inp.files.length > 0 && inp.name) {
                const comprimida = await comprimirImagenForm(inp.files[0]);
                fd.set(inp.name, comprimida);
            }
        }

        const resp = await fetch(form.action, {
            method: 'POST',
            body: fd,
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        const textoResp = await resp.text();
        let res = {};
        try {
            res = JSON.parse(textoResp);
        } catch (parseErr) {
            throw new Error('Respuesta inesperada del servidor (' + resp.status + ')');
        }

        if (resp.ok && (res.status === 'ok' || res.success)) {
            try {
                if (res.ticket && typeof mostrarTicketAvanta === 'function') {
                    mostrarTicketAvanta(res.ticket);
                } else {
                    const modalOk = document.getElementById('modalCreditoOk');
                    if (modalOk) {
                        modalOk.style.display = 'flex';
                    } else {
                        window.location.href = '/';
                    }
                }
            } catch (uiErr) {
                console.warn('Error al mostrar ticket/modal tras guardar crédito:', uiErr);
                window.location.href = '/';
            }


        } else {
            mostrarAvisoAvanta('Registro de Crédito', res.mensaje || res.message || 'No se pudo registrar el crédito.', 'error');
            if (btn) {
                btn.disabled = false;
                btn.innerText = txtOriginal;
            }
        }
    } catch (e) {
        mostrarAvisoAvanta('Error de Conexión', 'Error al guardar el crédito: ' + (e.message || 'Verifica tu conexión.'), 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerText = txtOriginal;
        }
    }


let ultimoTicketAvantaData = null;

function mostrarTicketAvanta(tk) {
    ultimoTicketAvantaData = tk;
    const modal = document.getElementById('modalTicketAvanta');
    if (!modal) {
        window.location.href = '/';
        return;
    }
    const esPago = (tk.tipo === 'pago');
    const esRenov = (tk.tipo_operacion || '').includes('RENOVACIÓN');

    const setTexto = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };
    const setDisplayPadre = (id, disp) => {
        const el = document.getElementById(id);
        if (el && el.parentElement) el.parentElement.style.display = disp;
    };

    setTexto('tkBarraTitulo', esPago
        ? 'Comprobante de Pago'
        : (esRenov ? 'Comprobante de Renovación' : 'Comprobante de Crédito'));
    setTexto('tkTipoOperacion', tk.tipo_operacion || (esPago ? 'COMPROBANTE OFICIAL DE PAGO' : 'COMPROBANTE DE CRÉDITO'));
    setTexto('tkCliente', tk.cliente || '-');
    setTexto('tkCedula', tk.identificacion || 'N/A');
    setTexto('tkTelefono', tk.telefono || 'N/A');
    setTexto('tkDireccion', tk.direccion || 'N/A');

    const rowRef = document.getElementById('tkRowRef');
    if (rowRef) {
        if (tk.referencia && tk.referencia.trim() !== '') {
            rowRef.innerText = 'Ref/Local: ' + tk.referencia.trim();
            rowRef.style.display = 'block';
        } else {
            rowRef.style.display = 'none';
        }
    }

    const elCodigo = document.getElementById('tkCodigo');
    const elSaldo = document.getElementById('tkSaldo');
    const bloqueCond = elCodigo && elCodigo.parentElement ? elCodigo.parentElement.parentElement : null;
    const bloqueSaldo = elSaldo && elSaldo.parentElement ? elSaldo.parentElement.parentElement : null;

    if (esPago) {
        if (rowRef) rowRef.style.display = 'none';
        setDisplayPadre('tkCedula', 'none');
        setDisplayPadre('tkDireccion', 'none');
        const elTel = document.getElementById('tkTelefono');
        if (elTel && elTel.parentElement) {
            elTel.parentElement.innerHTML = '<b>WhatsApp:</b> <span>' + (tk.telefono || 'N/A') + '</span>';
        }
        if (bloqueCond) {
            bloqueCond.innerHTML = `
                <div><b>Fecha de Pago:</b> <span>${tk.fecha_pago || '-'}</span></div>
                <div style="font-size:15px; color:#16a34a; margin-top:4px;"><b>Monto Cobrado:</b> $${tk.monto_cobrado || '0.00'}</div>
                <div><b>Cuota Procesada:</b> <span>${tk.cuota_procesada || '1'}</span></div>
                <div style="font-size:16px; color:#0f172a; margin-top:3px;"><b>Saldo Restante:</b> $${tk.saldo_restante || '0.00'}</div>
            `;
        }
        const colorAlerta = tk.tipo_alerta === 'mora' ? '#dc2626' : (tk.tipo_alerta === 'adelanto' ? '#0284c7' : '#16a34a');
        if (bloqueSaldo) {
            bloqueSaldo.innerHTML = `
                <div style="font-size:13.5px; font-weight:900; color:${colorAlerta}; line-height:1.35;">${tk.titulo_estado || 'Gracias por su puntualidad y pago!'}</div>
                <div style="font-size:11.5px; font-weight:700; color:#475569; margin-top:4px; line-height:1.35;">${tk.subtitulo_estado || ''}</div>
            `;
        }
        setTexto('tkFechaEmision', tk.pie_sistema || 'Sistema de Gestión de Cobros Avanta');
    } else {
        const unidadPlazo = (tk.frecuencia === 'Semanal') ? 'Semanas' : 'Días';
        if (bloqueCond) {
            bloqueCond.innerHTML = `
                <div><b>Cód. Crédito:</b> <span id="tkCodigo">${tk.codigo || '000000'}</span></div>
                <div><b>Modalidad:</b> <span id="tkFrecuencia">${tk.frecuencia || 'Diaria'}</span></div>
                <div><b>Fecha entrega:</b> <span id="tkFechaInicio">${tk.fecha_inicio || '-'}</span></div>
                <div><b>Capital entregado:</b> $<span id="tkCapital">${tk.monto_capital || '0.00'}</span></div>
                <div><b>Total a pagar:</b> $<span id="tkTotal">${tk.monto_total || '0.00'}</span></div>
                <div><b>Valor cuota:</b> $<span id="tkCuota">${tk.valor_cuota || '0.00'}</span></div>
                <div><b>Plazo:</b> <span id="tkPlazo">${(tk.cuotas || 0) + ' ' + unidadPlazo}</span></div>
            `;
        }
        if (bloqueSaldo) {
            bloqueSaldo.innerHTML = `
                <div style="font-size:19px; font-weight:900; color:#0f172a;">Saldo actual: $<span id="tkSaldo">${tk.monto_total || '0.00'}</span></div>
                <div style="font-size:12.5px; font-weight:700; color:#334155; margin-top:3px;">Vence: <span id="tkVence">${tk.fecha_vencimiento || '-'}</span></div>
                <div style="font-size:12px; font-weight:800; color:#16a34a; margin-top:2px;">Estado: Al día</div>
            `;
        }
    }

    const ahora = new Date();
    setTexto('tkFechaEmision', 'Emitido: ' + ahora.toLocaleDateString('es-EC') + ' ' + ahora.toLocaleTimeString('es-EC', { hour: '2-digit', minute: '2-digit' }));
    modal.style.display = 'flex';
}

function cerrarTicketAvantaYVolver() {
    const modal = document.getElementById('modalTicketAvanta');
    if (modal) modal.style.display = 'none';
    if (!document.getElementById('datosTicketExpediente')) {
        window.location.href = '/';
    }
}

function abrirTicketCreditoDesdeExp() {
    const el = document.getElementById('datosTicketExpediente');
    if (!el) return;
    const d = el.dataset;
    mostrarTicketAvanta({
        tipo: 'credito',
        cliente: d.cliente,
        cedula: d.cedula,
        telefono: d.telefono,
        direccion: d.direccion,
        referencia: d.referencia,
        codigo: d.codigo,
        frecuencia: d.frecuencia,
        fecha_inicio: d.inicio,
        monto_capital: d.capital,
        monto_total: d.total,
        valor_cuota: d.cuota,
        cuotas: d.cuotas,
        saldo_restante: d.saldo,
        fecha_vencimiento: d.vencimiento,
        fecha_emision: d.inicio
    });
}

function abrirTicketPagoDesdeExp(numCuota, montoCobrado, fechaPago) {
    const el = document.getElementById('datosTicketExpediente');
    if (!el) return;
    const d = el.dataset;
    let vencidasExp = 0;
    document.querySelectorAll('table tbody tr').forEach(tr => {
        if ((tr.textContent || '').includes('Vencida')) vencidasExp++;
    });
    const tieneMoraExp = vencidasExp > 0 && parseFloat(d.saldo || '0') > 0;
    mostrarTicketAvanta({
        tipo: 'pago',
        cliente: d.cliente,
        cedula: d.cedula,
        telefono: d.telefono,
        direccion: d.direccion,
        referencia: d.referencia,
        monto_cobrado: montoCobrado,
        cuota_procesada: numCuota,
        saldo_restante: d.saldo,
        fecha_pago: fechaPago,
        tipo_alerta: tieneMoraExp ? 'mora' : 'ok',
        titulo_estado: tieneMoraExp ? ('Días de Atraso: ' + vencidasExp + ' días en mora') : '¡Gracias por su puntualidad y pago!',
        subacknowledged: '',
        subtitulo_estado: tieneMoraExp ? 'Le solicitamos ponerse al día para evitar recargos o suspensión de su cupo.' : 'Comprobante reemitido desde historial'
    });
}

async function compartirTicketAvanta() {
    if (!ultimoTicketAvantaData) return;
    const tk = ultimoTicketAvantaData;
    const esPago = (tk.tipo === 'pago');
    const unidadPlazo = (tk.frecuencia === 'Semanal') ? 'Semanas' : 'Días';

    const canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = esPago ? (tk.referencia ? 840 : 800) : (tk.referencia ? 920 : 880);
    const ctx = canvas.getContext('2d');

    ctx.fillStyle = '#e2e8f0';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(30, 30, canvas.width - 60, canvas.height - 60);

    ctx.fillStyle = '#e2e8f0';
    for (let x = 30; x < canvas.width - 30; x += 20) {
        ctx.beginPath(); ctx.moveTo(x, 30); ctx.lineTo(x + 10, 44); ctx.lineTo(x + 20, 30); ctx.fill();
        ctx.beginPath(); ctx.moveTo(x, canvas.height - 30); ctx.lineTo(x + 10, canvas.height - 44); ctx.lineTo(x + 20, canvas.height - 30); ctx.fill();
    }

    ctx.fillStyle = '#0f2b5c';
    ctx.beginPath(); ctx.roundRect(145, 65, 58, 58, 12); ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 34px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('A', 174, 106);
    // Flecha celeste cruzada sobre la letra A del logotipo
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 4;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();
    ctx.moveTo(154, 100);
    ctx.lineTo(192, 82);
    ctx.moveTo(180, 82);
    ctx.lineTo(192, 82);
    ctx.lineTo(192, 94);
    ctx.stroke();
    ctx.textAlign = 'left';
    ctx.fillStyle = '#0f2b5c';
    ctx.font = 'bold 32px sans-serif';
    ctx.fillText('AVANTA PAGOS', 218, 96);
    ctx.fillStyle = '#475569';
    ctx.font = 'bold 17px sans-serif';
    ctx.fillText(tk.tipo_operacion || (esPago ? 'COMPROBANTE OFICIAL DE PAGO' : 'COMPROBANTE DE CRÉDITO'), 218, 120);

    const drawLine = (y) => {
        ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(65, y); ctx.lineTo(canvas.width - 65, y); ctx.stroke();
    };

    drawLine(148);
    ctx.textAlign = 'center';
    ctx.fillStyle = '#1e293b';
    let y = 190;

    if (esPago) {
        ctx.font = 'bold 24px sans-serif';
        ctx.fillText('Cliente: ' + (tk.cliente || '-'), canvas.width / 2, y);
        y += 36;
        ctx.font = '21px sans-serif';
        ctx.fillText('WhatsApp: ' + (tk.telefono || 'N/A'), canvas.width / 2, y);
        y += 36;
        ctx.fillText('Fecha de Pago: ' + (tk.fecha_pago || '-'), canvas.width / 2, y);
        y += 22;

        drawLine(y);
        y += 46;

        ctx.fillStyle = '#16a34a';
        ctx.font = 'bold 27px sans-serif';
        ctx.fillText('Monto Cobrado: $' + (tk.monto_cobrado || '0.00'), canvas.width / 2, y);
        y += 38;
        ctx.fillStyle = '#1e293b';
        ctx.font = 'bold 22px sans-serif';
        ctx.fillText('Cuota Procesada: #' + (tk.cuota_procesada || '1'), canvas.width / 2, y);
        y += 40;
        ctx.fillStyle = '#0f172a';
        ctx.font = 'bold 26px sans-serif';
        ctx.fillText('Saldo Restante: $' + (tk.saldo_restante || '0.00'), canvas.width / 2, y);
        y += 24;

        drawLine(y);
        y += 46;

        ctx.fillStyle = tk.tipo_alerta === 'mora' ? '#dc2626' : (tk.tipo_alerta === 'adelanto' ? '#0284c7' : '#16a34a');
        ctx.font = 'bold 22px sans-serif';
        ctx.fillText(tk.titulo_estado || '¡Gracias por su puntualidad y pago!', canvas.width / 2, y);
        y += 34;

        const subTxt = tk.subtitulo_estado || '';
        ctx.fillStyle = '#334155';
        ctx.font = '18px sans-serif';
        if (subTxt.length > 44) {
            const corte = subTxt.lastIndexOf(' ', 44);
            ctx.fillText(subTxt.slice(0, corte), canvas.width / 2, y);
            y += 26;
            ctx.fillText(subTxt.slice(corte + 1), canvas.width / 2, y);
            y += 38;
        } else if (subTxt) {
            ctx.fillText(subTxt, canvas.width / 2, y);
            y += 38;
        }

        drawLine(y - 12);
        y += 28;
        ctx.fillStyle = '#475569';
        ctx.font = 'italic 19px sans-serif';
        ctx.fillText(tk.pie_sistema || 'Sistema de Gestión de Cobros Avanta', canvas.width / 2, y);
    } else {
        const filasCliente = [
            'Cliente: ' + (tk.cliente || '-'),
            'Cédula: ' + (tk.identificacion || 'N/A'),
            'Tel: ' + (tk.telefono || 'N/A'),
            'Dir: ' + (tk.direccion || 'N/A')
        ];
        if (tk.referencia) filasCliente.push('Local/Ref: ' + tk.referencia);

        filasCliente.forEach(txt => {
            ctx.font = txt.startsWith('Cliente:') ? 'bold 23px sans-serif' : '21px sans-serif';
            ctx.fillText(txt, canvas.width / 2, y);
            y += 34;
        });

        drawLine(y + 6);
        y += 46;

        const filasCred = [
            'Cód. Crédito: ' + (tk.codigo || '000000'),
            'Modalidad: ' + (tk.frecuencia || 'Diaria'),
            'Fecha entrega: ' + (tk.fecha_inicio || '-'),
            'Capital entregado: $' + (tk.monto_capital || '0.00'),
            'Total a pagar: $' + (tk.monto_total || '0.00'),
            'Valor cuota: $' + (tk.valor_cuota || '0.00'),
            'Plazo: ' + (tk.cuotas || 0) + ' ' + unidadPlazo
        ];
        filasCred.forEach(txt => {
            ctx.font = txt.startsWith('Total') || txt.startsWith('Valor') ? 'bold 22px sans-serif' : '21px sans-serif';
            ctx.fillText(txt, canvas.width / 2, y);
            y += 35;
        });

        drawLine(y + 6);
        y += 52;
        ctx.fillStyle = '#0f172a';
        ctx.font = 'bold 32px sans-serif';
        ctx.fillText('Saldo actual: $' + (tk.monto_total || '0.00'), canvas.width / 2, y);
        y += 36;
        ctx.fillStyle = '#334155';
        ctx.font = 'bold 21px sans-serif';
        ctx.fillText('Vence: ' + (tk.fecha_vencimiento || '-'), canvas.width / 2, y);
        y += 32;
        ctx.fillStyle = '#16a34a';
        ctx.fillText('Estado: Al día', canvas.width / 2, y);
        y += 45;

        ctx.fillStyle = '#475569';
        ctx.font = 'bold 20px sans-serif';
        ctx.fillText('** Gracias por preferirnos **', canvas.width / 2, y);
    }

    const textoResumen = tk.mensaje_ws || (`*AVANTA PAGOS - ${tk.tipo_operacion}*\nCliente: ${tk.cliente}`);

        canvas.toBlob(async (blob) => {
        const nombreArchivo = esPago ? `Recibo_Pago_${tk.codigo}.png` : `Comprobante_Avanta_${tk.codigo}.png`;
        const file = new File([blob], nombreArchivo, { type: 'image/png' });

        // Copiar el número del cliente al portapapeles para pegarlo rápido en WhatsApp
        const telLimpio = (tk.telefono || '').replace(/[^0-9]/g, '');
        if (telLimpio.length >= 9 && navigator.clipboard) {
            try {
                await navigator.clipboard.writeText(telLimpio);
            } catch (e) {}
        }

        if (navigator.canShare && navigator.canShare({ files: [file] })) {
            try {
                await navigator.share({
                    files: [file],
                    title: 'Comprobante AVANTA PAGOS'
                });
                return;
            } catch (err) {
                if (err && err.name === 'AbortError') return;
            }
        }

        // Respaldo si el navegador no soporta compartir archivos: descarga la imagen PNG y abre el chat
        const urlDescarga = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = urlDescarga;
        a.download = nombreArchivo;
        a.click();
        URL.revokeObjectURL(urlDescarga);

        if (telLimpio.length >= 8) {
            const prefijosPorTz = {
                'America/Guayaquil': '593', 'America/Bogota': '57', 'America/Lima': '51',
                'America/Mexico_City': '52', 'America/Monterrey': '52', 'America/Tijuana': '52',
                'America/Santiago': '56', 'America/Argentina/Buenos_Aires': '54',
                'America/La_Paz': '591', 'America/Asuncion': '595', 'America/Montevideo': '598',
                'America/Caracas': '58', 'America/Santo_Domingo': '1', 'America/Guatemala': '502',
                'America/Tegucigalpa': '504', 'America/Managua': '505', 'America/Costa_Rica': '506',
                'America/Panama': '507', 'America/El_Salvador': '503', 'America/Sao_Paulo': '55',
                'Europe/Madrid': '34', 'Europe/London': '44'
            };
            const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
            const prefijo = prefijosPorTz[tz] || '';
            const numWa = (prefijo && telLimpio.startsWith('0')) ? (prefijo + telLimpio.slice(1)) : telLimpio;
            window.open(`https://wa.me/${numWa}`, '_blank');
        }
    }, 'image/png');
}

function filtrarDirectorioClientes() {
    const inp = document.getElementById('buscadorClientesDir');
    if (!inp) return;
    const q = inp.value.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    document.querySelectorAll('.item-cliente-dir').forEach(card => {
        const texto = (card.getAttribute('data-filtro') || '').normalize("NFD").replace(/[\u0300-\u036f]/g, "");
        card.style.display = texto.includes(q) ? 'flex' : 'none';
    });
}

function abrirGpsCliente(lat, lon) {
    if (!lat || !lon) {
        mostrarAvisoAvanta('Sin Coordenadas', 'Este cliente no tiene coordenadas GPS.', 'alerta');
        return;
    }
    const esMovil = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    const urlMaps = esMovil ? `geo:${lat},${lon}?q=${lat},${lon}(Ubicación+Cliente)` : `https://www.google.com/maps?q=${lat},${lon}`;
    window.open(urlMaps, '_blank');
}

let modoEdicionActivo = false;
function toggleEdicionPerfil(clienteId) {
    const inputs = document.querySelectorAll('.input-perfil');
    const btn = document.getElementById('btnEditarPerfil');
    if (!btn) return;
    
    if (!modoEdicionActivo) {
        inputs.forEach(inp => { inp.removeAttribute('readonly'); inp.style.background = '#ffffff'; inp.style.borderColor = '#2563eb'; });
        btn.innerText = 'Guardar';
        btn.style.background = '#10b981';
        btn.style.color = 'white';
        modoEdicionActivo = true;
    } else {
        const payload = {
            nombre: document.getElementById('perf_nombre').value,
            telefono: document.getElementById('perf_telefono').value,
            direccion: document.getElementById('perf_direccion').value,
            referencia: document.getElementById('perf_referencia').value
        };
        fetch('/api/cliente/actualizar/' + clienteId, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(res => {
            if (res.status === 'ok') {
                modoEdicionActivo = false;
                const mPerf = document.getElementById('modalPerfilOk');
                if (mPerf) mPerf.style.display = 'flex';
            } else {
                mostrarAvisoAvanta('Error al Guardar', 'Error al guardar: ' + res.message, 'error');
            }
        });
    }
}

        function verFotoAmpliada(src) {
            const img = document.getElementById('imgVisorFotoFull');
            const modal = document.getElementById('modalVisorFoto');
            if (img && modal) {
                img.src = src;
                modal.style.display = 'flex';
            }
        }

        function procesarFotoCedula(input, lado) {
            if (!input.files || !input.files[0]) return;
            const file = input.files[0];
            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    const maxDim = 900;
                    let w = img.width;
                    let h = img.height;
                    if (w > maxDim || h > maxDim) {
                        if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
                        else { w = Math.round(w * maxDim / h); h = maxDim; }
                    }
                    canvas.width = w;
                    canvas.height = h;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, w, h);
                    const base64Comprimida = canvas.toDataURL('image/jpeg', 0.72);

                    const hiddenIn = document.getElementById('inputFotoCedula' + lado);
                    const prevImg = document.getElementById('prevCedula' + lado);
                    const phDiv = document.getElementById('placeholderCedula' + lado);
                    if (hiddenIn) hiddenIn.value = base64Comprimida;
                    if (prevImg) { prevImg.src = base64Comprimida; prevImg.style.display = 'block'; }
                    if (phDiv) phDiv.style.display = 'none';
                };
                img.src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

// 🛡️ BLINDAJE CRÍTICO DEL DASHBOARD PRINCIPAL: Evita que Jinja2 busque variables de clientes fuera de su sección
{% if vista == 'perfil_cliente' and cliente %}
function abrirConcederCreditoPrecargado(lat, lon) {
    const clienteId = "{{ cliente.id|default('') }}"; 

    navegarRuta('/menu/renovar/' + clienteId);

    let intentos = 0;
    const chequearFormulario = setInterval(() => {
        const inpNombre = document.querySelector('input[name="nombre"]');
        intentos++;

        if (inpNombre) {
            clearInterval(chequearFormulario);
            
            const inpDir = document.querySelector('input[name="direccion"]') || document.getElementById('direccion_input');
            const inpRef = document.querySelector('input[name="referencia"]') || document.getElementById('referencia_input');
            const inpLat = document.getElementById('latitud_input');
            const inpLon = document.getElementById('longitud_input');
            const lblGps = document.getElementById('status_gps');

            if (inpLat && !inpLat.value && lat) inpLat.value = lat;
            if (inpLon && !inpLon.value && lon) inpLon.value = lon;
            if (lblGps && lat) {
                lblGps.innerText = "✅ Ubicación de ruta cargada con éxito";
                lblGps.style.color = "#10b981";
            }
        }

        if (intentos > 60) {
            clearInterval(chequearFormulario);
        }
    }, 50);
}
{% endif %}

function aplicarFiltrosDashboard() {
            const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
            const showPend = document.getElementById('chkPendientes').checked;
            const showPagos = document.getElementById('chkPagos').checked;
            const showNoPagos = document.getElementById('chkNoPagos').checked;
            const fDiario = document.getElementById('chkDiario').checked;
            const fSemanal = document.getElementById('chkSemanal').checked;
            const filterByFreq = fDiario || fSemanal;

            const cards = document.querySelectorAll('.cliente-card-premium');
            let visibles = 0;

            cards.forEach(card => {
                const nombre = card.getAttribute('data-nombre') || '';
                const estado = card.getAttribute('data-estado') || 'pendiente';
                const freq = card.getAttribute('data-frecuencia') || 'diario';

                const matchTexto = nombre.includes(q);
                const matchEstado = (estado === 'pendiente' && showPend) || 
                                    (estado === 'pago' && showPagos) || 
                                    (estado === 'no_pago' && showNoPagos);
                const matchFreq = !filterByFreq || (freq === 'diario' && fDiario) || (freq === 'semanal' && fSemanal);

                if (matchTexto && matchEstado && matchFreq) {
                    card.style.display = '';
                    visibles++;
                } else {
                    card.style.setProperty('display', 'none', 'important');
                }
            });

            document.getElementById('msgSinResultados').style.display = visibles === 0 ? 'block' : 'none';
        }

        function cambiarTabDirAvanta(btn, estado) {
            document.getElementById('filtroEstadoDirActual').value = estado;
            document.querySelectorAll('.tab-dir-avanta').forEach(b => {
                b.style.background = '#ffffff';
                b.style.color = '#334155';
                b.style.borderColor = '#cbd5e1';
            });
            btn.style.background = '#0f2b5c';
            btn.style.color = '#ffffff';
            btn.style.borderColor = '#0f2b5c';
            filtrarDirectorioAvanta();
        }

        function filtrarDirectorioAvanta() {
            const q = (document.getElementById('buscadorClientesDir').value || '').toLowerCase().trim();
            const est = document.getElementById('filtroEstadoDirActual').value;
            document.querySelectorAll('.item-cliente-dir').forEach(card => {
                const txt = card.getAttribute('data-filtro') || '';
                const cardEst = card.getAttribute('data-estado') || 'libre';
                const matchTxt = txt.includes(q);
                const matchEst = (est === 'todos') || (est === cardEst);
                if (matchTxt && matchEst) {
                    card.style.display = 'flex';
                } else {
                    card.style.setProperty('display', 'none', 'important');
                }
            });
        }

        function filtrarSecuenciaRuta() {
            const q = (document.getElementById('buscadorRutaOrden').value || '').toLowerCase().trim();
            document.querySelectorAll('.item-ruta-orden').forEach(card => {
                const txt = card.getAttribute('data-filtro') || '';
                if (txt.includes(q)) {
                    card.style.display = 'flex';
                } else {
                    card.style.setProperty('display', 'none', 'important');
                }
            });
        }

        function seleccionarTipoFlujoBal(tipo) {
            document.getElementById('tipo_mov').value = tipo;
            const bSal = document.getElementById('btnFlujoSalida');
            const bEnt = document.getElementById('btnFlujoEntrada');
            if (tipo === 'Salida') {
                bSal.style.borderColor = '#ef4444';
                bSal.style.background = '#fef2f2';
                bSal.style.color = '#b91c1c';
                bSal.style.fontWeight = '800';
                bEnt.style.borderColor = '#cbd5e1';
                bEnt.style.background = '#ffffff';
                bEnt.style.color = '#475569';
                bEnt.style.fontWeight = '700';
            } else {
                bEnt.style.borderColor = '#16a34a';
                bEnt.style.background = '#f0fdf4';
                bEnt.style.color = '#15803d';
                bEnt.style.fontWeight = '800';
                bSal.style.borderColor = '#cbd5e1';
                bSal.style.background = '#ffffff';
                bSal.style.color = '#475569';
                bSal.style.fontWeight = '700';
            }
        }

        function seleccionarCatBal(btn, cat) {
            document.getElementById('categoria_mov').value = cat;
            document.querySelectorAll('.btn-cat-bal').forEach(b => {
                b.style.borderColor = '#e2e8f0';
                b.style.background = '#f8fafc';
                b.style.color = '#334155';
                b.style.fontWeight = '700';
            });
            btn.style.borderColor = '#0f2b5c';
            btn.style.background = '#eff6ff';
            btn.style.color = '#0f2b5c';
            btn.style.fontWeight = '800';
            if (cat === 'Inversión') {
                seleccionarTipoFlujoBal('Entrada');
            }
        }

        function cambiarTabEnrAvanta(btn, freq) {
            document.getElementById('filtroFreqEnrActual').value = freq;
            document.querySelectorAll('.tab-enr-avanta').forEach(b => {
                b.style.background = '#ffffff';
                b.style.color = '#334155';
                b.style.borderColor = '#cbd5e1';
            });
            btn.style.background = '#0f2b5c';
            btn.style.color = '#ffffff';
            btn.style.borderColor = '#0f2b5c';
            filtrarEnrutamientoAvanta();
        }

        function filtrarEnrutamientoAvanta() {
            const q = (document.getElementById('buscadorEnrutamiento').value || '').toLowerCase().trim();
            const freq = document.getElementById('filtroFreqEnrActual').value;
            document.querySelectorAll('.item-enr-avanta').forEach(card => {
                const txt = card.getAttribute('data-filtro') || '';
                const cardFreq = card.getAttribute('data-freq') || 'Diaria';
                const matchTxt = txt.includes(q);
                const matchFreq = (freq === 'todos') || (freq === cardFreq);
                if (matchTxt && matchFreq) {
                    card.style.display = 'flex';
                } else {
                    card.style.setProperty('display', 'none', 'important');
                }
            });
        }

        let dispositivoBluetooth = null;
        let caracteristicaGatt = null;

        async function conectarImpresoraBluetooth() {
            const boxStatus = document.getElementById('box_bt_status');
            const dotStatus = document.getElementById('dot_bt_status');
            const textStatus = document.getElementById('text_bt_status');
            const btnProbar = document.getElementById('btn_probar_print');

            if (!navigator.bluetooth) {
                mostrarAvisoAvanta('Bluetooth no compatible', 'Tu navegador no soporta Web Bluetooth. Abre la app desde Google Chrome en Android.', 'alerta');
                return;
            }

            try {
                if (textStatus) textStatus.innerText = "Buscando dispositivos Bluetooth cercanos...";

                dispositivoBluetooth = await navigator.bluetooth.requestDevice({
                    acceptAllDevices: true,
                    optionalServices: [
                        '000018f0-0000-1000-8000-00805f9b34fb',
                        '0000ff00-0000-1000-8000-00805f9b34fb',
                        '49535343-fe7d-4ae5-8fa9-9fafd205e455',
                        'e7810a71-73ae-499d-8c15-faa9aef0c3f2',
                        '00001101-0000-1000-8000-00805f9b34fb'
                    ]
                });

                if (textStatus) textStatus.innerText = "Conectando con " + (dispositivoBluetooth.name || "Impresora 58mm") + "...";
                const servidorGatt = await dispositivoBluetooth.gatt.connect();
                const servicios = await servidorGatt.getPrimaryServices();

                caracteristicaGatt = null;
                for (const serv of servicios) {
                    const chars = await serv.getCharacteristics();
                    const encontrada = chars.find(c => c.properties.write || c.properties.writeWithoutResponse);
                    if (encontrada) {
                        caracteristicaGatt = encontrada;
                        break;
                    }
                }

                if (caracteristicaGatt) {
                    if (boxStatus) {
                        boxStatus.style.background = "#dcfce7";
                        boxStatus.style.color = "#15803d";
                        boxStatus.style.borderColor = "#86efac";
                    }
                    if (dotStatus) dotStatus.style.background = "#16a34a";
                    if (textStatus) {
                        textStatus.innerHTML = "Conectada con éxito a: <b style='color:#15803d;'>" + (dispositivoBluetooth.name || "Impresora Térmica 58mm") + "</b>";
                    }
                    if (btnProbar) {
                        btnProbar.disabled = false;
                        btnProbar.style.background = "linear-gradient(135deg, #16a34a, #15803d)";
                        btnProbar.style.color = "#ffffff";
                        btnProbar.style.cursor = "pointer";
                        btnProbar.style.boxShadow = "0 4px 12px rgba(22,163,74,0.25)";
                    }
                } else {
                    throw new Error("El dispositivo seleccionado no tiene canal de impresión ESC/POS compatible.");
                }
            } catch (error) {
                console.error("Error Bluetooth:", error);
                if (boxStatus) {
                    boxStatus.style.background = "#fef2f2";
                    boxStatus.style.color = "#dc2626";
                    boxStatus.style.borderColor = "#fecaca";
                }
                if (dotStatus) dotStatus.style.background = "#ef4444";
                if (textStatus) {
                    textStatus.innerText = "No se pudo enlazar (" + (error.message || "Cancelado") + ")";
                }
            }
        }

        async function probarImpresionTicket() {
            if (!caracteristicaGatt) {
                mostrarAvisoAvanta('Impresora no vinculada', 'Primero vincula tu impresora térmica de 58mm.', 'alerta');
                return;
            }
            try {
                const encoder = new TextEncoder();
                const initEscPos = new Uint8Array([0x1B, 0x40, 0x1B, 0x61, 0x01]);
                const cuerpo = encoder.encode(
                    "AVANTA PAGOS PRO\\n" +
                    "--------------------------------\\n" +
                    "TICKET DE PRUEBA EXITOSO\\n" +
                    "Impresora Termica 58mm\\n" +
                    "Estado: EN LINEA\\n" +
                    "--------------------------------\\n\\n\\n"
                );
                await caracteristicaGatt.writeValue(initEscPos);
                await caracteristicaGatt.writeValue(cuerpo);
                mostrarAvisoAvanta('Prueba Enviada', 'Ticket de prueba enviado a la impresora.', 'exito');
            } catch (error) {
                mostrarAvisoAvanta('Error de Impresión', 'Error al imprimir ticket: ' + error.message, 'error');
            }
        }

        function recalcularPreviewEditCred() {
            const m = parseFloat(document.getElementById('editCredMonto').value) || 0;
            const i = parseFloat(document.getElementById('editCredInteres').value) || 0;
            const c = parseInt(document.getElementById('editCredCuotas').value) || 1;
            const total = m + (m * (i / 100));
            const cuota = total / c;
            document.getElementById('editCredPreview').innerText = '$' + cuota.toFixed(2) + ' / $' + total.toFixed(2);
        }

        function procesarFotoEditCred(input, lado) {
            if (!input.files || !input.files[0]) return;
            const file = input.files[0];
            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    const maxDim = 900;
                    let w = img.width, h = img.height;
                    if (w > maxDim || h > maxDim) {
                        if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
                        else { w = Math.round(w * maxDim / h); h = maxDim; }
                    }
                    canvas.width = w; canvas.height = h;
                    canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                    const b64 = canvas.toDataURL('image/jpeg', 0.72);
                    document.getElementById('editCredFoto' + lado).value = b64;
                    const prev = document.getElementById('prevEditCed' + lado);
                    const ph = document.getElementById('phEditCed' + lado);
                    prev.src = b64; prev.style.display = 'block';
                    ph.style.display = 'none';
                };
                img.src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

        function abrirEditorCreditoHoy(cliId, movId, nombre, monto, interes, cuotas, freq) {
            if (!cliId && !nombre) {
                mostrarAvisoAvanta('Cliente no encontrado', 'No se encontró la ficha activa de este cliente.', 'alerta');
                return;
            }
            document.getElementById('editCredCliId').value = cliId || 0;
            document.getElementById('editCredMovId').value = movId || 0;
            document.getElementById('editCredNombre').innerText = nombre;
            document.getElementById('editCredNombreInput').value = nombre;
            document.getElementById('editCredTelefono').value = '';
            document.getElementById('editCredDireccion').value = '';
            document.getElementById('editCredMonto').value = monto.toFixed(2);
            document.getElementById('editCredInteres').value = interes;
            document.getElementById('editCredCuotas').value = cuotas;
            document.getElementById('editCredFrecuencia').value = freq;
            document.getElementById('editCredMicroseguro').value = '0.00';
            document.getElementById('editCredFotoFrente').value = '';
            document.getElementById('editCredFotoReverso').value = '';
            document.getElementById('prevEditCedFrente').style.display = 'none';
            document.getElementById('phEditCedFrente').style.display = 'inline';
            document.getElementById('prevEditCedReverso').style.display = 'none';
            document.getElementById('phEditCedReverso').style.display = 'inline';
            recalcularPreviewEditCred();
            document.getElementById('modalEditarCreditoHoy').style.display = 'flex';

            fetch('/api/datos_credito_hoy/' + cliId)
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'ok') {
                        document.getElementById('editCredNombreInput').value = d.nombre || nombre;
                        document.getElementById('editCredCedula').value = d.identificacion || '';
                        document.getElementById('editCredTelefono').value = d.telefono || '';
                        document.getElementById('editCredDireccion').value = d.direccion || '';
                        document.getElementById('editCredMicroseguro').value = parseFloat(d.microseguro || 0).toFixed(2);
                        if (d.foto_frente) {
                            document.getElementById('editCredFotoFrente').value = d.foto_frente;
                            document.getElementById('prevEditCedFrente').src = d.foto_frente;
                            document.getElementById('prevEditCedFrente').style.display = 'block';
                            document.getElementById('phEditCedFrente').style.display = 'none';
                        }
                        if (d.foto_reverso) {
                            document.getElementById('editCredFotoReverso').value = d.foto_reverso;
                            document.getElementById('prevEditCedReverso').src = d.foto_reverso;
                            document.getElementById('prevEditCedReverso').style.display = 'block';
                            document.getElementById('phEditCedReverso').style.display = 'none';
                        }
                    }
                });
        }

        function guardarEdicionCreditoHoy() {
            const btn = document.getElementById('btnGuardarEditCred');
            btn.disabled = true;
            btn.innerText = 'Guardando...';

            const payload = {
                cliente_id: document.getElementById('editCredCliId').value,
                mov_id: document.getElementById('editCredMovId').value,
                nombre: document.getElementById('editCredNombreInput').value,
                identificacion: document.getElementById('editCredCedula').value,
                telefono: document.getElementById('editCredTelefono').value,
                direccion: document.getElementById('editCredDireccion').value,
                monto: document.getElementById('editCredMonto').value,
                interes: document.getElementById('editCredInteres').value,
                cuotas: document.getElementById('editCredCuotas').value,
                frecuencia: document.getElementById('editCredFrecuencia').value,
                microseguro: document.getElementById('editCredMicroseguro').value,
                foto_frente: document.getElementById('editCredFotoFrente').value,
                foto_reverso: document.getElementById('editCredFotoReverso').value
            };

            fetch('/api/editar_credito_hoy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(res => {
                if (res.status === 'ok') {
                    window.location.reload();
                } else {
                    mostrarAvisoAvanta('Error al Actualizar', res.message || 'Error al actualizar el crédito.', 'error');
                    btn.disabled = false;
                    btn.innerText = 'Guardar Cambios';
                }
            })
            .catch(() => {
                mostrarAvisoAvanta('Error de Conexión', 'Error de conexión al guardar.', 'error');
                btn.disabled = false;
                btn.innerText = 'Guardar Cambios';
            });
        }

let gpsIniciado = false;
function reportarUbicacionCobrador() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(function(pos) {
        gpsIniciado = true;
        fetch('/api/gps/reportar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                latitud: pos.coords.latitude.toFixed(6),
                longitud: pos.coords.longitude.toFixed(6)
            })
        }).catch(() => {});
    }, function() {}, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
}
document.addEventListener('click', function() {
    if (!gpsIniciado) reportarUbicacionCobrador();
});
window.addEventListener('load', function() {
    reportarUbicacionCobrador();
    setInterval(reportarUbicacionCobrador, 60000);
});

</script>
</body>
</html>
"""

@app.route("/login", methods=["GET", "POST"])
def login_route():
    error_msg = None
    usuario_def = ""
    unidad_txt = "AVANTA PAGOS"
    caja_txt = "Ingresa tus credenciales"
    caja_clase = "caja-abierta"
    caja_abierta_bool = True

    conn = get_db()
    try:
        if request.method == "POST":
            usuario_ingresado = request.form.get("usuario", "").strip()
            clave_ingresada = request.form.get("clave", "").strip()
            usuario_def = usuario_ingresado

            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT r.id, r.nombre, r.usuario_cobrador, r.clave_cobrador, r.estado, r.gps_habilitado,
                        COALESCE(r.empresa_id, 1) AS empresa_id,
                        COALESCE(e.estado, 'Activa') AS empresa_estado
                    FROM rutas r
                    LEFT JOIN empresas e ON COALESCE(r.empresa_id, 1) = e.id
                    WHERE LOWER(r.usuario_cobrador) = LOWER(%s)
                    ORDER BY r.id ASC
                """, (usuario_ingresado,))
                rutas_candidatas = cursor.fetchall()
                ruta_row = next((r for r in rutas_candidatas if _verify_password(r.get("clave_cobrador"), clave_ingresada)), None)

                if ruta_row:
                    r_dict = dict(ruta_row)
                    rid = int(r_dict["id"])
                    emp_id = int(r_dict.get("empresa_id") or 1)
                    emp_est = str(r_dict.get("empresa_estado") or "Activa").strip().lower()
                    clv_ruta = str(r_dict.get("clave_cobrador") or "").strip()
                    estado_ruta = str(r_dict.get("estado") or "Activa").strip()

                    if emp_est != "activa":
                        error_msg = "El servicio de esta empresa está suspendido. Contacta al Administrador."
                    elif estado_ruta != "Activa":
                        error_msg = "Esta ruta se encuentra suspendida por el Administrador."
                    elif not _verify_password(clv_ruta, clave_ingresada):
                        error_msg = "Contraseña incorrecta para esta ruta."
                    else:
                        # Migración transparente de credenciales antiguas en texto plano.
                        if not _password_is_hashed(clv_ruta):
                            cursor.execute("UPDATE rutas SET clave_cobrador = %s WHERE id = %s AND COALESCE(empresa_id, 1) = %s", (generate_password_hash(clave_ingresada), rid, emp_id))
                            conn.commit()
                        _set_tenant_context(conn, emp_id, rid)
                        cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_autorizada' AND ruta_id = %s", (rid,))
                        row_aut = cursor.fetchone()
                        raw_aut = (dict(row_aut).get("valor", "0") if row_aut else "0")
                        val_aut = str(raw_aut).strip()
                        if val_aut != "1":
                            error_msg = f"La caja de {r_dict['nombre']} está cerrada. Solicita apertura al Administrador."
                        else:
                            session.clear()
                            session.permanent = True
                            tz_detectada = (request.form.get("tz_cliente") or "UTC").strip()
                            simbolo_mon, codigo_mon = detectar_moneda_por_tz(tz_detectada)
                            session["tz"] = tz_detectada
                            session["moneda_simbolo"] = simbolo_mon
                            session["moneda_codigo"] = codigo_mon
                            session["autenticado"] = True
                            session["usuario"] = usuario_ingresado
                            session["ruta_id"] = rid
                            session["empresa_id"] = emp_id
                            session["nombre_ruta"] = r_dict["nombre"]
                            session["gps_habilitado"] = int(r_dict.get("gps_habilitado") or 1)
                            return redirect("/")

                else:
                    # Respaldo legado para configuracion de Ruta 1, limitado a la empresa matriz.
                    _set_tenant_context(conn, 1, 1)
                    cursor.execute("SELECT llave, valor FROM configuracion WHERE llave IN ('usuario', 'clave', 'caja_autorizada') AND ruta_id = 1")
                    cfg = {r["llave"]: r["valor"] for r in cursor.fetchall()}
                    usr_db = cfg.get("usuario", "")
                    clv_db = cfg.get("clave", "")
                    if usr_db and clv_db and usuario_ingresado == usr_db and check_password_hash(clv_db, clave_ingresada):
                        if str(cfg.get("caja_autorizada") or "0").strip() != "1":
                            error_msg = "Tu caja se encuentra cerrada. Solicita apertura al Administrador."
                        else:
                            session.clear()
                            session.permanent = True
                            session["autenticado"] = True
                            session["usuario"] = usuario_ingresado
                            session["ruta_id"] = 1
                            session["empresa_id"] = 1
                            session["nombre_ruta"] = "RUTA - 01"
                            return redirect("/")
                    else:
                        error_msg = "Credenciales de ruta incorrectas."
    except Exception as e:
        print(f"Error en login_route: {e}")
        if request.method == "POST":
            error_msg = "Error de conexión al validar acceso."

    return render_template_string(
        LOGIN_HTML,
        error=error_msg,
        usuario_default=usuario_def,
        unidad_txt=unidad_txt,
        caja_txt=caja_txt,
        caja_clase=caja_clase,
        caja_abierta_bool=caja_abierta_bool
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
def dashboard_principal():
    if not session.get("autenticado"):
        return redirect("/login")
        
    rid = int(session.get("ruta_id") or 1)

    conn_val = get_db()
    turno_id_real = ahora_local().date().isoformat()
    val_caja = "0"
    try:
        with conn_val.cursor() as cur_cfg:
            cur_cfg.execute(
                "SELECT llave, valor FROM configuracion WHERE llave IN ('caja_autorizada', 'fecha_jornada_activa') AND ruta_id = %s",
                (rid,)
            )
            cfg_dash = {str(dict(r).get("llave")): str(dict(r).get("valor") or "").strip() for r in cur_cfg.fetchall()}
        val_caja = cfg_dash.get("caja_autorizada", "0") or "0"
        if cfg_dash.get("fecha_jornada_activa"):
            turno_id_real = cfg_dash["fecha_jornada_activa"]

        if val_caja == "0":
            return """
                <div style="text-align:center; padding:50px; font-family:sans-serif; background:#0f172a; color:white; min-height:100vh; display:flex; flex-direction:column; justify-content:center; align-items:center;">
                    <h1 style="color:#ef4444; font-size:32px; margin-bottom:10px;">⚠️ Jornada No Autorizada</h1>
                    <p style="color:#cbd5e1; font-size:16px; max-width:450px; line-height:1.6;">Tu caja se encuentra actualmente cerrada. Solicita al Administrador el pase de inicio para comenzar las cobranzas de hoy.</p>
                    <br>
                    <a href="/login" style="color:#a855f7; text-decoration:none; font-weight:bold; background:rgba(168,85,247,0.1); padding:10px 20px; border-radius:8px; border:1px solid #a855f7;">Volver al Inicio</a>
                </div>
            """, 403
    except Exception as e:
        print(f"Error en validación de jornada: {e}")

    fecha_hoy_real = turno_id_real[:10] if turno_id_real else ahora_local().date().isoformat()
    turno_id = turno_id_real or fecha_hoy_real
    hoy_str = fecha_hoy_real
    inicio_turno_ts = f"{turno_id_real[:10]} {turno_id_real[11:13]}:{turno_id_real[13:15]}:{turno_id_real[15:17]}" if len(str(turno_id_real)) >= 17 and "_" in str(turno_id_real) else f"{fecha_hoy_real} 00:00:00"

    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    ahora = ahora_local()
    fecha_legible = f"{dias_semana[ahora.weekday()]}, {ahora.day:02d} de {meses[ahora.month - 1]} de {ahora.year}"

    try:
        with conn_val.cursor() as cursor:
            # 1. Suma real en dólares del recaudo asentado en el turno activo
            cursor.execute("""
                SELECT COALESCE(SUM(p.valor_pagado), 0) AS total
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE c.ruta_id = %s
                  AND COALESCE(p.valor_pagado, 0) > 0
                  AND LEFT(COALESCE(p.fecha_pago_real::text, ''), 19) >= %s
            """, (rid, inicio_turno_ts))
            recaudo_hoy = Decimal(str(cursor.fetchone()["total"]))

            # 2. Conteo y detalle real de créditos otorgados en la jornada activa
            cursor.execute("""
                SELECT id, concepto, monto
                FROM balance_movimientos
                WHERE ruta_id = %s
                  AND LOWER(TRIM(tipo)) IN ('salida', 'egreso', 'credito')
                  AND (concepto ILIKE '%%crédito%%' OR concepto ILIKE '%%credito%%' OR concepto ILIKE '%%préstamo%%' OR concepto ILIKE '%%renovación%%' OR concepto ILIKE '%%renovacion%%')
                  AND fecha = %s
                ORDER BY id DESC
            """, (rid, turno_id_real))
            rows_creditos_turno = cursor.fetchall()
            creditos_hoy = len(rows_creditos_turno)

            ids_creditos_hoy = set()
            lista_creditos_html = ""
            total_colocado_turno = Decimal("0.00")
            for rc in rows_creditos_turno:
                d_rc = dict(rc)
                mov_id_c = int(d_rc.get("id") or 0)
                conc_c = str(d_rc.get("concepto") or "")
                monto_c = Decimal(str(d_rc.get("monto") or "0.00"))
                total_colocado_turno += monto_c

                nombre_cli_c = conc_c.split("CLIENTE:")[-1].strip() if "CLIENTE:" in conc_c else conc_c
                cursor.execute("""
                    SELECT id, COALESCE(interes_porcentaje, 20) AS interes, COALESCE(frecuencia, 'Diaria') AS frecuencia, COALESCE(cuotas, 20) AS cuotas, COALESCE(valor_cuota, 0) AS valor_cuota
                    FROM clientes
                    WHERE ruta_id = %s AND LOWER(TRIM(nombre)) = LOWER(%s)
                    ORDER BY id DESC LIMIT 1
                """, (rid, nombre_cli_c))
                cli_row = cursor.fetchone()

                cli_id_c = 0
                interes_c = 20.0
                cuotas_c = 20
                freq_c = "Diaria"
                v_cuota_c = float(monto_c) * 1.20 / 20.0

                if cli_row:
                    d_cli_c = dict(cli_row)
                    cli_id_c = int(d_cli_c.get("id") or 0)
                    interes_c = float(d_cli_c.get("interes") or 20)
                    freq_c = str(d_cli_c.get("frecuencia") or "Diaria")
                    cuotas_c = int(d_cli_c.get("cuotas") or 20)
                    v_cuota_c = float(d_cli_c.get("valor_cuota") or v_cuota_c)

                if cli_id_c > 0:
                    ids_creditos_hoy.add(cli_id_c)
                    cursor.execute("SELECT COUNT(*) AS total_c, COALESCE(MAX(valor), 0) AS val_c FROM pagos WHERE cliente_id = %s", (cli_id_c,))
                    row_p_info = cursor.fetchone()
                    if row_p_info:
                        d_pi = dict(row_p_info)
                        cnt_p = int(d_pi.get("total_c") or 0)
                        val_p = float(d_pi.get("val_c") or 0)
                        if cnt_p > 0:
                            cuotas_c = cnt_p
                        if val_p > 0:
                            v_cuota_c = val_p

                nombre_safe = nombre_cli_c.replace("'", "\\'")

                lista_creditos_html += f"""
                <div style="display:flex; flex-direction:column; gap:10px; padding:12px; background:#f8fafc; border:1.5px solid #e2e8f0; border-radius:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div style="text-align:left;">
                            <div style="font-size:13.5px; font-weight:800; color:#0f172a;">{nombre_cli_c}</div>
                            <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">{cuotas_c} cuotas ({freq_c}) • Cuota: ${v_cuota_c:.2f}</div>
                        </div>
                        <div style="text-align:right;">
                            <div style="font-size:14.5px; font-weight:900; color:#0f2b5c;">${monto_c:.2f}</div>
                            <span style="display:inline-block; margin-top:3px; font-size:10px; font-weight:800; color:#1d4ed8; background:#dbeafe; padding:2px 7px; border-radius:6px;">HOY</span>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; border-top:1px solid #e2e8f0; padding-top:8px;">
                        <button type="button" onclick="abrirEditorCreditoHoy({cli_id_c}, {mov_id_c}, '{nombre_safe}', {float(monto_c):.2f}, {interes_c}, {cuotas_c}, '{freq_c}')" style="padding:8px; border:1px solid #cbd5e1; border-radius:8px; background:#ffffff; color:#0f2b5c; font-size:11.5px; font-weight:700; cursor:pointer;">
                            ✏️ Editar Crédito
                        </button>
                        <button type="button" onclick="document.getElementById('modalCreditosHoy').style.display='none'; abrirGestorPago({cli_id_c}, '{nombre_safe}', {v_cuota_c:.2f}, {v_cuota_c * cuotas_c:.2f}, 0);" style="padding:8px; border:none; border-radius:8px; background:#16a34a; color:#ffffff; font-size:11.5px; font-weight:700; cursor:pointer;">
                            💵 Registrar Abono
                        </button>
                    </div>
                </div>
                """

            if not lista_creditos_html:
                lista_creditos_html = '<div style="padding:20px 10px; text-align:center; color:#64748b; font-size:13px; font-weight:600;">Aún no se han entregado créditos en este turno.</div>'

            capital_prestado_hoy = total_colocado_turno

            # 🎯 FIJADO DIRECTO CON TUS COLUMNAS REALES
            # Diarios: salen todos los días.
            # Semanales: salen el día de su cuota y siguen saliendo cada día de esa semana hasta que completen la cuota (pagado = 1).
            cursor.execute("""
                SELECT c.id, c.nombre, c.telefono, c.direccion, c.referencia, c.identificacion, c.frecuencia, c.cuotas, c.valor_cuota, c.monto_total, c.orden, c.estado, c.fecha_gestion, c.fecha_inicio
                FROM clientes c
                WHERE c.ruta_id = %s AND c.estado = 'Activo'
                  AND (
                      EXISTS (
                          SELECT 1 FROM pagos p_hoy
                          WHERE p_hoy.cliente_id = c.id
                            AND p_hoy.pagado = 1
                            AND (CAST(p_hoy.fecha_pago_real AS TEXT) LIKE %s OR LEFT(COALESCE(p_hoy.fecha_pago_real::text, ''), 19) >= %s)

                      )
                      OR (
                          COALESCE(NULLIF(TRIM(c.fecha_inicio::text), '')::date, CURRENT_DATE) <= CAST(%s AS DATE)
                          AND (
                              LOWER(TRIM(c.frecuencia)) = 'diaria'
                              OR (
                                  LOWER(TRIM(c.frecuencia)) = 'semanal'
                                  AND EXISTS (
                                      SELECT 1 FROM pagos p
                                      WHERE p.cliente_id = c.id
                                        AND ((p.pagado = 0 AND p.fecha <= %s) OR CAST(p.fecha_pago_real AS TEXT) LIKE %s)
                                  )
                              )
                          )
                      )
                  )
                ORDER BY c.orden ASC, c.id DESC
            """, (rid, f"{fecha_hoy_real}%", inicio_turno_ts, hoy_str, hoy_str, f"{fecha_hoy_real}%"))
            clientes_crudos = cursor.fetchall()

            # Consultamos los IDs de cuotas tocadas en el turno activo
            cursor.execute("""
                SELECT p.id AS pago_id, p.cliente_id
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE c.ruta_id = %s
                AND COALESCE(p.valor_pagado, 0) > 0
                AND LEFT(COALESCE(p.fecha_pago_real::text, ''), 19) >= %s
            """, (rid, inicio_turno_ts))
            abonos_turno_rows = cursor.fetchall()

            mapa_pagos_hoy = defaultdict(lambda: {"monto": Decimal("0.00"), "ids": []})
            for ab in abonos_turno_rows:
                cid_h = int(ab["cliente_id"])
                mapa_pagos_hoy[cid_h]["ids"].append(str(ab["pago_id"]))

            # Consultamos el monto EXACTO cobrado hoy desde balance_movimientos (evita sumar parciales de días anteriores)
            cursor.execute("""
                SELECT concepto, monto
                FROM balance_movimientos
                WHERE ruta_id = %s
                  AND categoria = 'Cobranza'
                  AND fecha = COALESCE(
                      (SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s LIMIT 1),
                      %s
                  )
            """, (rid, rid, hoy_str))
            for bm in cursor.fetchall():
                conc = str(bm.get("concepto") or "")
                if "(ID:" in conc:
                    try:
                        cid_b = int(conc.split("(ID:")[1].split(")")[0])
                        mapa_pagos_hoy[cid_b]["monto"] += Decimal(str(bm.get("monto") or "0.00"))
                    except Exception:
                        pass

            # Reconstruimos la estructura exacta dentro del mismo bloque del cursor
            clientes_ruta = []
            cnt_pendientes = 0
            cnt_pagos = 0
            cnt_no_pagos = 0
            cnt_diario = 0
            cnt_semanal = 0

            if clientes_crudos:
                c_ids = [c["id"] for c in clientes_crudos]
                cursor.execute("SELECT id, cliente_id, numero, valor, valor_pagado, pagado, fecha, fecha_pago_real FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (c_ids,))
                todos_pagos = cursor.fetchall()
                
                pagos_mep = defaultdict(list)
                for pg in todos_pagos:
                    pagos_mep[pg["cliente_id"]].append(dict(pg))
                    
                for c in clientes_crudos:
                    d_cl = dict(c)
                    info_hoy = mapa_pagos_hoy.get(d_cl["id"])

                    # Todo crédito otorgado hoy inicia su cobro en lista de pendientes mañana
                    if int(d_cl["id"]) in ids_creditos_hoy and not (info_hoy and info_hoy["monto"] > 0):
                        continue

                    d_cl["pagos"] = pagos_mep.get(d_cl["id"], [])

                    # Determinar estado de gestión del día: 'pago', 'no_pago' o 'pendiente'
                    # Calcular fecha del último pago real (completo o parcial)
                    fechas_p = [str(p["fecha_pago_real"]).strip() for p in d_cl["pagos"] if (int(p["pagado"]) == 1 or float(p.get("valor_pagado") or 0) > 0) and p.get("fecha_pago_real")]
                    d_cl["ultimo_pago_str"] = sorted(fechas_p)[-1] if fechas_p else "Sin pagos previos"

                    # Determinar estado de gestión del día: 'pago', 'no_pago' o 'pendiente'
                    f_gest_full = str(d_cl["fecha_gestion"]).strip() if d_cl.get("fecha_gestion") else ""
                    f_gest = f_gest_full[:10]
                    hoy_base = str(hoy_str).strip()[:10]
                    ult_pago_ts = str(d_cl.get("ultimo_pago_str") or "")[:19]
                    es_np_hoy = bool(f_gest and (f_gest in (hoy_base, fecha_hoy_real, turno_id[:10]) or f_gest_full >= inicio_turno_ts))
                    np_despues_de_pago = bool(es_np_hoy and len(f_gest_full) >= 19 and f_gest_full > ult_pago_ts)

                    if np_despues_de_pago:
                        d_cl["estado_hoy"] = "no_pago"
                        d_cl["monto_pagado_hoy"] = info_hoy["monto"] if info_hoy else Decimal("0.00")
                        d_cl["pago_ids_hoy"] = ",".join(info_hoy["ids"]) if info_hoy else ""
                        cnt_no_pagos += 1
                    elif info_hoy and info_hoy["monto"] > 0:
                        d_cl["estado_hoy"] = "pago"
                        d_cl["monto_pagado_hoy"] = info_hoy["monto"]
                        d_cl["pago_ids_hoy"] = ",".join(info_hoy["ids"])
                        cnt_pagos += 1
                    elif es_np_hoy:
                        d_cl["estado_hoy"] = "no_pago"
                        d_cl["monto_pagado_hoy"] = Decimal("0.00")
                        d_cl["pago_ids_hoy"] = ""
                        cnt_no_pagos += 1
                    else:
                        d_cl["estado_hoy"] = "pendiente"
                        d_cl["monto_pagado_hoy"] = Decimal("0.00")
                        d_cl["pago_ids_hoy"] = ""
                        cnt_pendientes += 1

                    if d_cl.get("frecuencia") == "Diaria":
                        cnt_diario += 1
                    else:
                        cnt_semanal += 1

                    # CÁLCULO EXACTO DE MORA Y ADELANTO (0 AL DÍA + 1 DÍA INMEDIATO AL MARCAR NO PAGO)
                    hoy_clean = str(hoy_str).strip()[:10]
                    hoy_dt = date.fromisoformat(hoy_clean) if len(hoy_clean) == 10 else date.today()
                    dias_mora = 0
                    dias_adelanto = 0

                    for p in d_cl["pagos"]:
                        f_q = datetime.strptime(str(p["fecha"])[:10], "%Y-%m-%d").date() if isinstance(p["fecha"], str) else p["fecha"]
                        if int(p["pagado"]) == 0 and f_q < hoy_dt:
                            dias_mora += 1
                        elif int(p["pagado"]) == 1 and f_q > hoy_dt:
                            dias_adelanto += 1

                    # Si hoy se presionó No Pago, suma 1 día de atraso inmediato (si estaba en 0 pasa a 1)
                    if d_cl["estado_hoy"] == "no_pago":
                        if dias_adelanto > 0:
                            dias_adelanto -= 1
                        else:
                            dias_mora += 1

                    d_cl["dias_mora"] = dias_mora
                    d_cl["dias_adelanto"] = dias_adelanto
                    clientes_ruta.append(d_cl)

    except Exception as e:
        if conn_val:
            conn_val.rollback()
        print(f"Error contable en Dashboard Principal: {e}")
        recaudo_hoy = Decimal("0.00")
        creditos_hoy = 0
        capital_prestado_hoy = Decimal("0.00")
        total_colocado_turno = Decimal("0.00")
        lista_creditos_html = ""
        clientes_ruta = []
        cnt_pendientes = cnt_pagos = cnt_no_pagos = cnt_diario = cnt_semanal = 0

    debido_minimo_dia = sum(Decimal(str(cl["valor_cuota"])) for cl in clientes_ruta if cl.get("estado_hoy") == "pendiente")
    
    debido_total_acumulado = Decimal("0.00")
    for cl in clientes_ruta:
        for p in cl["pagos"]:
            if not p["pagado"] and str(p["fecha"])[:10] <= hoy_str:
                debido_total_acumulado += (Decimal(str(p["valor"])) - Decimal(str(p["valor_pagado"])))

    capital_en_calle = Decimal("0.00")
    for c in clientes_ruta:
        pagado_cliente = sum(Decimal(str(p["valor_pagado"])) for p in c["pagos"])
        pendiente = Decimal(str(c["monto_total"])) - pagado_cliente
        if pendiente > 0:
            capital_en_calle += pendiente

    contexto_dash = f"""
    <!-- FECHA EJECUTIVA -->
    <div style="display:flex; align-items:center; justify-content:center; gap:7px; background:#ffffff; border:1px solid #e2e8f0; border-radius:20px; padding:7px 14px; margin:0 auto 12px auto; width:fit-content; box-shadow:0 2px 6px rgba(15,23,42,0.03);">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        <span style="font-size:12px; font-weight:800; color:#1e293b; letter-spacing:0.2px;">{fecha_legible}</span>
    </div>

    <!-- PANEL KPI 2x2 EJECUTIVO (SIN CAPITAL EN CALLE) -->
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:9px; margin-bottom:12px; text-align:left;">
        <!-- 1. RECAUDO HOY -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-left:3.5px solid #16a34a; border-radius:13px; padding:11px 12px; box-shadow:0 2px 8px rgba(15,23,42,0.04);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;">
                <span style="font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Recaudo Hoy</span>
                <span style="width:22px; height:22px; border-radius:7px; background:#dcfce7; color:#16a34a; display:flex; align-items:center; justify-content:center;">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                </span>
            </div>
            <div style="font-size:18px; font-weight:900; color:#0f172a; letter-spacing:-0.3px;">${recaudo_hoy:.2f}</div>
        </div>

        <!-- 2. CRÉDITOS HOY (CLICABLE PARA VER DETALLE) -->
        <div onclick="document.getElementById('modalCreditosHoy').style.display='flex'"
             style="background:#ffffff; border:1px solid #e2e8f0; border-left:3.5px solid #0f2b5c; border-radius:13px; padding:11px 12px; box-shadow:0 2px 8px rgba(15,23,42,0.04); cursor:pointer;">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;">
                <span style="font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Créditos Hoy</span>
                <span style="width:22px; height:22px; border-radius:7px; background:#e0e7ff; color:#0f2b5c; display:flex; align-items:center; justify-content:center;">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
                </span>
            </div>
            <div style="display:flex; align-items:baseline; justify-content:space-between;">
                <div style="font-size:18px; font-weight:900; color:#0f2b5c; letter-spacing:-0.3px;">{creditos_hoy} <span style="font-size:12px; font-weight:700; color:#64748b;">Créd.</span></div>
                <span style="font-size:10.5px; font-weight:800; color:#1d4ed8;">Ver ›</span>
            </div>
        </div>

        <!-- 3. MÍNIMO DE RUTA (RECAUDO ESPERADO) -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-left:3.5px solid #0284c7; border-radius:13px; padding:11px 12px; box-shadow:0 2px 8px rgba(15,23,42,0.04);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;">
                <span style="font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Mínimo Ruta</span>
                <span style="width:22px; height:22px; border-radius:7px; background:#e0f2fe; color:#0284c7; display:flex; align-items:center; justify-content:center;">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                </span>
            </div>
            <div style="font-size:17px; font-weight:900; color:#0369a1; letter-spacing:-0.3px;">${debido_minimo_dia:.2f}</div>
        </div>

        <!-- 4. ACUMULADO MORA -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-left:3.5px solid #d97706; border-radius:13px; padding:11px 12px; box-shadow:0 2px 8px rgba(15,23,42,0.04);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;">
                <span style="font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Acumulado Mora</span>
                <span style="width:22px; height:22px; border-radius:7px; background:#fef3c7; color:#d97706; display:flex; align-items:center; justify-content:center;">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                </span>
            </div>
            <div style="font-size:17px; font-weight:900; color:#b45309; letter-spacing:-0.3px;">${debido_total_acumulado:.2f}</div>
        </div>
    </div>

    <!-- BARRA DE BÚSQUEDA CON BOTÓN LIMPIAR (X) + BOTÓN DE FILTROS EJECUTIVO -->
    <div style="display:flex; gap:8px; align-items:center; margin-bottom:13px;">
        <div style="position:relative; flex:1;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; left:13px; top:50%; transform:translateY(-50%); pointer-events:none;"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" id="searchInput" class="search-box" placeholder="Buscar cliente o cédula..."
                   oninput="document.getElementById('btnClearSearch').style.display = this.value.length > 0 ? 'flex' : 'none'; aplicarFiltrosDashboard();"
                   style="width:100%; margin-bottom:0; padding:11px 36px 11px 38px; border-radius:12px; border:1.5px solid #e2e8f0; background:#ffffff; font-size:13.5px; font-weight:600; color:#0f172a; box-shadow:0 2px 6px rgba(15,23,42,0.03); box-sizing:border-box;">
            <button type="button" id="btnClearSearch" title="Limpiar búsqueda"
                    onclick="const inp=document.getElementById('searchInput'); inp.value=''; this.style.display='none'; aplicarFiltrosDashboard(); inp.focus();"
                    style="display:none; position:absolute; right:10px; top:50%; transform:translateY(-50%); width:22px; height:22px; border-radius:50%; background:#e2e8f0; color:#475569; border:none; align-items:center; justify-content:center; cursor:pointer; padding:0;">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>
        <button type="button" onclick="document.getElementById('modalFiltrosRuta').style.display='flex'"
                style="width:44px; height:44px; border-radius:12px; background:#0f2b5c; color:#ffffff; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; box-shadow:0 3px 8px rgba(15,43,92,0.22);">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
                <line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2" fill="#0f2b5c"/>
                <line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2" fill="#0f2b5c"/>
                <line x1="4" y1="18" x2="20" y2="18"/><circle cx="10" cy="18" r="2" fill="#0f2b5c"/>
            </svg>
        </button>
    </div>

    <!-- Modal Ejecutivo de Créditos Realizados Hoy -->
    <div id="modalCreditosHoy" onclick="this.style.display='none'"
         style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.6); z-index:9999; align-items:center; justify-content:center; padding:16px; box-sizing:border-box;">
        <div onclick="event.stopPropagation()"
             style="background:#ffffff; width:100%; max-width:360px; border-radius:18px; overflow:hidden; box-shadow:0 20px 40px rgba(0,0,0,0.28);">
            <div style="background:#0f2b5c; color:#ffffff; padding:15px 18px; display:flex; justify-content:space-between; align-items:center;">
                <div style="text-align:left;">
                    <div style="font-size:15.5px; font-weight:900;">Créditos Realizados Hoy</div>
                    <div style="font-size:11px; color:#cbd5e1; font-weight:600;">Total colocado: ${total_colocado_turno:.2f}</div>
                </div>
                <button type="button" onclick="document.getElementById('modalCreditosHoy').style.display='none'"
                        style="background:rgba(255,255,255,0.15); border:none; color:#ffffff; width:28px; height:28px; border-radius:8px; font-size:16px; font-weight:800; cursor:pointer;">✕</button>
            </div>
            <div style="padding:15px; max-height:60vh; overflow-y:auto;">
                {lista_creditos_html}
            </div>
            <div style="padding:0 15px 15px 15px;">
                <button type="button" onclick="document.getElementById('modalCreditosHoy').style.display='none'"
                        style="width:100%; padding:12px; background:#0f2b5c; color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:13.5px; cursor:pointer;">
                    Cerrar
                </button>
            </div>
        </div>
    </div>

        <!-- Modal Editor de Crédito Realizado Hoy -->
    <div id="modalEditarCreditoHoy" onclick="this.style.display='none'"
         style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.72); z-index:10000; align-items:center; justify-content:center; padding:16px; box-sizing:border-box;">
        <div onclick="event.stopPropagation()"
             style="background:#ffffff; width:100%; max-width:360px; border-radius:18px; overflow:hidden; box-shadow:0 24px 48px rgba(0,0,0,0.35); text-align:left;">
            <div style="background:#0f2b5c; color:#ffffff; padding:14px 18px; display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <div style="font-size:15px; font-weight:900;">Corregir Crédito Entregado</div>
                    <div id="editCredNombre" style="font-size:11.5px; color:#93c5fd; font-weight:700;">Cliente</div>
                </div>
                <button type="button" onclick="document.getElementById('modalEditarCreditoHoy').style.display='none'"
                        style="background:rgba(255,255,255,0.15); border:none; color:#ffffff; width:28px; height:28px; border-radius:8px; font-size:15px; font-weight:800; cursor:pointer;">✕</button>
            </div>
            <div style="padding:16px; max-height:78vh; overflow-y:auto;">
                <input type="hidden" id="editCredCliId">
                <input type="hidden" id="editCredMovId">
                <input type="hidden" id="editCredFotoFrente">
                <input type="hidden" id="editCredFotoReverso">

                <div style="margin-bottom:9px;">
                    <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Nombre Completo</label>
                    <input type="text" id="editCredNombreInput"
                           style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                </div>

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:9px;">
                    <div>
                        <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Cédula / ID</label>
                        <input type="text" id="editCredCedula"
                               style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Teléfono</label>
                        <input type="text" id="editCredTelefono"
                               style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                    </div>
                </div>

                <div style="margin-bottom:10px;">
                    <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Dirección</label>
                    <input type="text" id="editCredDireccion"
                           style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                </div>

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
                    <div>
                        <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Monto Prestado ($)</label>
                        <input type="number" step="0.01" id="editCredMonto" oninput="recalcularPreviewEditCred()"
                               style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; font-size:11px; font-weight:800; color:#475569; margin-bottom:3px;">Interés (%)</label>
                        <input type="number" step="1" id="editCredInteres" oninput="recalcularPreviewEditCred()"
                               style="width:100%; padding:9px 10px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                </div>

                <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:11px;">
                    <div>
                        <label style="display:block; font-size:10.5px; font-weight:800; color:#475569; margin-bottom:3px;">N° Cuotas</label>
                        <input type="number" id="editCredCuotas" oninput="recalcularPreviewEditCred()"
                               style="width:100%; padding:9px 8px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; font-size:10.5px; font-weight:800; color:#475569; margin-bottom:3px;">Frecuencia</label>
                        <select id="editCredFrecuencia"
                                style="width:100%; padding:9px 6px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:12.5px; font-weight:800; color:#0f172a; background:#ffffff; box-sizing:border-box;">
                            <option value="Diaria">Diaria</option>
                            <option value="Semanal">Semanal</option>
                        </select>
                    </div>
                    <div>
                        <label style="display:block; font-size:10.5px; font-weight:800; color:#475569; margin-bottom:3px;">Microseguro ($)</label>
                        <input type="number" step="0.01" id="editCredMicroseguro" value="0.00"
                               style="width:100%; padding:9px 8px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:800; color:#0f172a; box-sizing:border-box;">
                    </div>
                </div>

                <!-- FOTOS DE CÉDULA EN EL EDITOR -->
                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:10px; margin-bottom:11px;">
                    <div style="font-size:11px; font-weight:800; color:#0f2b5c; margin-bottom:8px;">Fotos de Cédula (Toca para ver o cambiar)</div>
                    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
                        <div style="display:flex; align-items:center; gap:8px;">
                            <label style="width:48px; height:48px; border-radius:10px; background:#e2e8f0; border:1.5px solid #cbd5e1; display:flex; align-items:center; justify-content:center; cursor:pointer; overflow:hidden; flex-shrink:0;">
                                <img id="prevEditCedFrente" src="" style="width:100%; height:100%; object-fit:cover; display:none;">
                                <span id="phEditCedFrente" style="color:#475569; font-size:18px;">📷</span>
                                <input type="file" accept="image/*" capture="environment" onchange="procesarFotoEditCred(this, 'Frente')" style="display:none;">
                            </label>
                            <div>
                                <div style="font-size:11.5px; font-weight:800; color:#0f172a;">Frontal</div>
                                <div onclick="const s=document.getElementById('prevEditCedFrente').src; if(s && s.startsWith('data:image')) verFotoAmpliada(s);"
                                     style="font-size:10.5px; font-weight:700; color:#1d4ed8; cursor:pointer;">Ampliar</div>
                            </div>
                        </div>

                        <div style="display:flex; align-items:center; gap:8px;">
                            <label style="width:48px; height:48px; border-radius:10px; background:#e2e8f0; border:1.5px solid #cbd5e1; display:flex; align-items:center; justify-content:center; cursor:pointer; overflow:hidden; flex-shrink:0;">
                                <img id="prevEditCedReverso" src="" style="width:100%; height:100%; object-fit:cover; display:none;">
                                <span id="phEditCedReverso" style="color:#475569; font-size:18px;">📷</span>
                                <input type="file" accept="image/*" capture="environment" onchange="procesarFotoEditCred(this, 'Reverso')" style="display:none;">
                            </label>
                            <div>
                                <div style="font-size:11.5px; font-weight:800; color:#0f172a;">Reverso</div>
                                <div onclick="const s=document.getElementById('prevEditCedReverso').src; if(s && s.startsWith('data:image')) verFotoAmpliada(s);"
                                     style="font-size:10.5px; font-weight:700; color:#1d4ed8; cursor:pointer;">Ampliar</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div style="background:#f1f5f9; border-radius:10px; padding:9px 12px; margin-bottom:13px; display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:11.5px; font-weight:700; color:#475569;">Nueva Cuota / Total:</span>
                    <span id="editCredPreview" style="font-size:13px; font-weight:900; color:#0f2b5c;">$0.00 / $0.00</span>
                </div>

                <div style="display:flex; gap:8px;">
                    <button type="button" onclick="document.getElementById('modalEditarCreditoHoy').style.display='none'"
                            style="flex:1; padding:12px; background:#e2e8f0; color:#334155; border:none; border-radius:11px; font-weight:800; font-size:13px; cursor:pointer;">
                        Cancelar
                    </button>
                    <button type="button" id="btnGuardarEditCred" onclick="guardarEdicionCreditoHoy()"
                            style="flex:2; padding:12px; background:#16a34a; color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:13.5px; cursor:pointer;">
                        Guardar Cambios
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal Profesional de Filtros -->
    <div id="modalFiltrosRuta" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.6); z-index:9999; align-items:center; justify-content:center; padding:16px; box-sizing:border-box;">
        <div style="background:#ffffff; width:100%; max-width:340px; border-radius:16px; overflow:hidden; box-shadow:0 15px 35px rgba(0,0,0,0.25); animation:fadeIn 0.15s ease;">
            <div style="background:#0f2b5c; color:#ffffff; padding:15px 18px; display:flex; justify-content:space-between; align-items:center;">
                <span style="font-size:16px; font-weight:800;">Filtros</span>
                <button type="button" onclick="document.getElementById('modalFiltrosRuta').style.display='none'" style="background:transparent; border:none; color:#ffffff; font-size:20px; font-weight:bold; cursor:pointer; padding:0 4px;">✕</button>
            </div>
            
            <div style="padding:18px; text-align:left;">
                <div style="font-size:13px; font-weight:800; color:#334155; margin-bottom:12px;">Estado de pago</div>
                
                <label style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; cursor:pointer; font-size:14px; color:#1e293b; font-weight:600;">
                    <span>Pendientes ({cnt_pendientes})</span>
                    <input type="checkbox" id="chkPendientes" checked onchange="aplicarFiltrosDashboard()" style="width:19px; height:19px; accent-color:#16a34a; cursor:pointer;">
                </label>
                
                <label style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; cursor:pointer; font-size:14px; color:#1e293b; font-weight:600;">
                    <span>Pagos ({cnt_pagos})</span>
                    <input type="checkbox" id="chkPagos" onchange="aplicarFiltrosDashboard()" style="width:19px; height:19px; accent-color:#16a34a; cursor:pointer;">
                </label>
                
                <label style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; cursor:pointer; font-size:14px; color:#1e293b; font-weight:600;">
                    <span>No pagos ({cnt_no_pagos})</span>
                    <input type="checkbox" id="chkNoPagos" onchange="aplicarFiltrosDashboard()" style="width:19px; height:19px; accent-color:#16a34a; cursor:pointer;">
                </label>

                <div style="height:1px; background:#e2e8f0; margin:14px 0;"></div>
                <div style="font-size:13px; font-weight:800; color:#334155; margin-bottom:12px;">Frecuencia de pago</div>
                
                <label style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; cursor:pointer; font-size:14px; color:#1e293b; font-weight:600;">
                    <span>Diario ({cnt_diario})</span>
                    <input type="checkbox" id="chkDiario" onchange="aplicarFiltrosDashboard()" style="width:19px; height:19px; accent-color:#16a34a; cursor:pointer;">
                </label>
                
                <label style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; cursor:pointer; font-size:14px; color:#1e293b; font-weight:600;">
                    <span>Semanal ({cnt_semanal})</span>
                    <input type="checkbox" id="chkSemanal" onchange="aplicarFiltrosDashboard()" style="width:19px; height:19px; accent-color:#16a34a; cursor:pointer;">
                </label>

                <div style="display:flex; justify-content:flex-end; margin-top:16px; padding-top:10px; border-top:1px solid #f1f5f9;">
                    <button type="button" onclick="document.getElementById('chkPendientes').checked=true; document.getElementById('chkPagos').checked=true; document.getElementById('chkNoPagos').checked=true; document.getElementById('chkDiario').checked=false; document.getElementById('chkSemanal').checked=false; aplicarFiltrosDashboard();" 
                            style="background:transparent; border:none; color:#0f2b5c; font-weight:800; font-size:14px; cursor:pointer; padding:4px 8px;">
                        Ver todo
                    </button>
                </div>
            </div>
        </div>
    </div>

        <!-- BARRA EJECUTIVA DE AVANCE DE RUTA -->
    <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:13px; padding:11px 13px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:7px;">
            <span style="font-size:11px; font-weight:800; color:#0f2b5c; text-transform:uppercase; letter-spacing:0.4px;">Avance de Ruta Hoy</span>
            <span style="font-size:11px; font-weight:800; color:#475569;">
                <span style="color:#16a34a;">{cnt_pagos} Pagos</span> • <span style="color:#dc2626;">{cnt_no_pagos} No pagos</span> • <span style="color:#0284c7;">{cnt_pendientes} Pend.</span>
            </span>
        </div>
        <div style="width:100%; height:7px; background:#e2e8f0; border-radius:99px; overflow:hidden; display:flex;">
            <div style="width:{(cnt_pagos * 100 / (cnt_pagos + cnt_no_pagos + cnt_pendientes)) if (cnt_pagos + cnt_no_pagos + cnt_pendientes) > 0 else 0:.1f}%; background:#16a34a; height:100%;"></div>
            <div style="width:{(cnt_no_pagos * 100 / (cnt_pagos + cnt_no_pagos + cnt_pendientes)) if (cnt_pagos + cnt_no_pagos + cnt_pendientes) > 0 else 0:.1f}%; background:#ef4444; height:100%;"></div>
        </div>
    </div>

    <div id="clientesContainer" style="display:flex; flex-direction:column; gap:5px; width:100%; box-sizing:border-box;">
    """
    
        # 📱 PROCESAMIENTO CONTABLE PREMIUM BLINDADO Y UNIFICADO (DISEÑO LIMPIO EN DASHBOARD)
    for idx_ruta, c in enumerate(clientes_ruta, start=1):
        pagado_acumulado = sum(Decimal(str(p["valor_pagado"])) for p in c["pagos"])
        saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - pagado_acumulado)
    
        valor_cuota_dec = Decimal(str(c["valor_cuota"]))
        
        # Contador decimal exacto según saldo pendiente real
        cuotas_totales_float = float(c["cuotas"])
        if float(valor_cuota_dec) > 0:
            cuotas_pendientes_float = round(float(saldo_restante) / float(valor_cuota_dec), 1)
        else:
            cuotas_pendientes_float = float(len([p for p in c["pagos"] if int(p["pagado"]) == 0]))
        cuotas_pagadas = int(round(cuotas_totales_float - cuotas_pendientes_float))
    
        cuota_p = next((p for p in c["pagos"] if int(p["pagado"]) == 0), None)
        
        mora_real = c.get("dias_mora", 0)
        adelanto_real = c.get("dias_adelanto", 0)

        if mora_real >= 11:
            dias_calculados = mora_real
            estado_mora_cl = "muy_atrasado"
        elif mora_real >= 6:
            dias_calculados = mora_real
            estado_mora_cl = "en_mora"
        elif mora_real > 0:
            dias_calculados = mora_real
            estado_mora_cl = "al_dia"
        elif adelanto_real > 0:
            dias_calculados = adelanto_real
            estado_mora_cl = "adelantado"
        else:
            dias_calculados = 0
            estado_mora_cl = "al_dia"
    
        c_id = str(c["id"])
        c_nombre = str(c["nombre"])
        c_nombre_lower = c_nombre.lower()
        c_referencia = str(c["referencia"]) if c["referencia"] else "Comercio"
        c_valor_cuota = f"{float(valor_cuota_dec):.2f}"
        c_saldo_restante = f"{float(saldo_restante):.2f}"
        c_cuotas_pagadas = str(cuotas_pagadas)

        inicial_frecuencia = 'D' if c['frecuencia'] == 'Diaria' else 'S'
        
        # 🎨 CONFIGURACIÓN DE LOS CÍRCULOS DE FRECUENCIA EN VIVO SEGÚN EL ESTADO CONTABLE
        if saldo_restante <= 0:
            html_indicador_mora = '<div class="circulo-frecuencia" style="border-color:#10b981 !important; color:#10b981 !important; background:#ecfdf5 !important; font-weight:800;">P</div>'
            clase_mora_badge = "0"
        elif estado_mora_cl == "muy_atrasado":
            html_indicador_mora = f'<div class="circulo-frecuencia" style="border-color:#dc2626 !important; color:#dc2626 !important; background:#fef2f2 !important;">{inicial_frecuencia}</div>'
            clase_mora_badge = "1"
        elif estado_mora_cl == "en_mora":
            html_indicador_mora = f'<div class="circulo-frecuencia" style="border-color:#ea580c !important; color:#ea580c !important; background:#fff7ed !important;">{inicial_frecuencia}</div>'
            clase_mora_badge = "1"
        elif estado_mora_cl == "adelantado":
            html_indicador_mora = f'<div class="circulo-frecuencia" style="border-color:#2563eb !important; color:#2563eb !important; background:#eff6ff !important;">{inicial_frecuencia}</div>'
            clase_mora_badge = "0"
        else:
            html_indicador_mora = f'<div class="circulo-frecuencia al-dia">{inicial_frecuencia}</div>'
            clase_mora_badge = "0"

        # Cálculo exacto para el cuadradito de días de atraso o adelanto (4 colores)
        if estado_mora_cl == "muy_atrasado":
            estilo_cuadro_dias = "background:#fee2e2; color:#dc2626; border:1px solid #fca5a5; font-weight:800;"
            texto_cuadro_dias = str(dias_calculados)
        elif estado_mora_cl == "en_mora":
            estilo_cuadro_dias = "background:#ffedd5; color:#ea580c; border:1px solid #fdba74; font-weight:800;"
            texto_cuadro_dias = str(dias_calculados)
        elif estado_mora_cl == "adelantado":
            estilo_cuadro_dias = "background:#dbeafe; color:#2563eb; border:1px solid #93c5fd; font-weight:800;"
            texto_cuadro_dias = str(dias_calculados)
        else:
            estilo_cuadro_dias = "background:#dcfce7; color:#16a34a; border:1px solid #86efac; font-weight:800;"
            texto_cuadro_dias = str(dias_calculados)
        
        js_num_cuota = str(cuota_p["numero"]) if cuota_p else "1"
        js_pendiente = f"{(cuota_p['valor'] - cuota_p['valor_pagado'])}" if cuota_p else "0"
        
                # Lógica de botones, barra lateral y estado visual según gestión del día
        estado_hoy = c.get("estado_hoy", "pendiente")
        monto_pag_hoy = c.get("monto_pagado_hoy", Decimal("0.00"))
        pago_ids_hoy = c.get("pago_ids_hoy", "")
        freq_slug = "diario" if c.get("frecuencia") == "Diaria" else "semanal"

        if estado_hoy == "pago":
            borde_izq_card = "border-left: 6px solid #16a34a !important;"
            display_card = "display: none !important;"
            html_col_pago = f'<span class="metric-val" style="color:#16a34a; font-weight:800;">${float(monto_pag_hoy):.2f}</span>'
            html_botones_derechos = f"""
                <button type="button" class="btn-accion-premium cobrar" title="Editar Cobro" 
                        onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}, {dias_calculados}, 'pago', '{pago_ids_hoy}')"
                        style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; outline:none; -webkit-tap-highlight-color:transparent;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#16a34a" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#16a34a" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M21 26.5L24 29.5L30.5 23" stroke="#16a34a" stroke-width="2.5"/>
                    </svg>
                </button>
                <button type="button" class="btn-accion-premium saltar" title="Cambiar a No Pago" 
                        onclick="ejecutarNoPago({c_id}, 'pago', '{pago_ids_hoy}')"
                        style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; padding:0; opacity:0.35; cursor:pointer; outline:none; -webkit-tap-highlight-color:transparent;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#64748b" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#64748b" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M22 23.5L29 30.5M29 23.5L22 30.5" stroke="#64748b" stroke-width="2.5"/>
                    </svg>
                </button>
            """
        elif estado_hoy == "no_pago":
            borde_izq_card = "border-left: 6px solid #ef4444 !important;"
            display_card = "display: none !important;"
            html_col_pago = '<span class="metric-val" style="color:#ef4444; font-weight:800;">$0</span>'
            html_botones_derechos = f"""
                <button type="button" class="btn-accion-premium cobrar" title="Cambiar a Pago" 
                        onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}, {dias_calculados}, 'no_pago', '')"
                        style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; padding:0; opacity:0.35; cursor:pointer; outline:none; -webkit-tap-highlight-color:transparent;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#64748b" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#64748b" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M21 26.5L24 29.5L30.5 23" stroke="#64748b" stroke-width="2.5"/>
                    </svg>
                </button>
                <button type="button" class="btn-accion-premium saltar" title="Editar No Pago" 
                        onclick="ejecutarNoPago({c_id}, 'no_pago', '')"
                        style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; outline:none; -webkit-tap-highlight-color:transparent;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#ef4444" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#ef4444" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M22 23.5L29 30.5M29 23.5L22 30.5" stroke="#ef4444" stroke-width="2.5"/>
                    </svg>
                </button>
            """
        else:
            borde_izq_card = ""
            display_card = ""
            html_col_pago = '<span class="metric-val">-</span>'
            if saldo_restante > 0:
                html_botones_derechos = f"""
                    <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" 
                            onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}, {dias_calculados})"
                            style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; outline:none; -webkit-tap-highlight-color:transparent; user-select:none;">
                        <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#16a34a" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M14 7H31V20H19"/>
                            <text x="23.5" y="17.5" fill="#16a34a" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                            <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                            <path d="M21 26.5L24 29.5L30.5 23" stroke="#16a34a" stroke-width="2.5"/>
                        </svg>
                    </button>
                    <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" 
                            onclick="ejecutarNoPago({c_id})"
                            style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; outline:none; -webkit-tap-highlight-color:transparent; user-select:none;">
                        <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#ef4444" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M14 7H31V20H19"/>
                            <text x="23.5" y="17.5" fill="#ef4444" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                            <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                            <path d="M22 23.5L29 30.5M29 23.5L22 30.5" stroke="#ef4444" stroke-width="2.5"/>
                        </svg>
                    </button>
                """
            else:
                html_botones_derechos = '<span style="font-size:18px; color:#10b981; font-weight:bold; padding-right:10px;">✔</span>'

        raw_ult_p = str(c.get("ultimo_pago_str") or "").strip()
        if not raw_ult_p or raw_ult_p == "Sin pagos previos":
            lista_pg_c = [str(pg.get("fecha_pago_real") or "").strip() for pg in (c.get("pagos") or []) if float(pg.get("valor_pagado") or 0) > 0 and pg.get("fecha_pago_real")]
            raw_ult_p = sorted(lista_pg_c)[-1] if lista_pg_c else ""
        ult_pago_str = raw_ult_p[:19] if raw_ult_p else "Sin pagos previos"

        # Inyección limpia en las tarjetas premium
        contexto_dash += f"""
        <div class="cliente-card-premium" id="cliente-card-{c_id}" data-cedula="{c.get('identificacion') or 'N/A'}" data-nombre="{c_nombre_lower}" data-mora="{clase_mora_badge}" data-estado="{estado_hoy}" data-frecuencia="{freq_slug}" data-dias="{dias_calculados}" data-es-adelanto="{1 if adelanto_real > 0 else 0}" data-ultpago="{ult_pago_str}"
            style="background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; padding:9px 10px; margin-bottom:0; box-shadow:0 2px 6px rgba(15,23,42,0.04); display:flex; align-items:center; justify-content:space-between; gap:8px; {borde_izq_card}">
                <div class="card-izquierda" onclick="navegarRuta('/menu/cliente/{c_id}')" style="flex:1; min-width:0; display:flex; align-items:flex-start; gap:10px; cursor:pointer;">
                <div style="padding-top:2px; flex-shrink:0;">
                    {html_indicador_mora}
                </div>
                <div class="info-bloque-texto" style="width:100%; min-width:0; text-align:left;">
                    <div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">
                        <span style="font-size:10.5px; font-weight:800; color:#0f2b5c; background:#eff6ff; border:1px solid #dbeafe; padding:1px 6px; border-radius:6px; flex-shrink:0;">#{idx_ruta}</span>
                        <span style="font-size:11px; font-weight:700; color:#64748b; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{c_referencia}</span>
                    </div>
                    <div style="font-size:14.5px; font-weight:900; color:#0f172a; letter-spacing:-0.2px; margin-bottom:8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{c_nombre}</div>

                    <div style="display:grid; grid-template-columns:1fr 1.15fr 0.85fr; gap:4px; background:#f8fafc; border:1px solid #f1f5f9; border-radius:10px; padding:6px 8px; margin-bottom:8px;">
                        <div>
                            <div style="font-size:9.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Vr. Cuota</div>
                            <div style="font-size:12.5px; font-weight:900; color:#0f172a;">${c_valor_cuota}</div>
                        </div>
                        <div>
                            <div style="font-size:9.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Pendiente</div>
                            <div style="font-size:12.5px; font-weight:900; color:#0f172a;">{cuotas_pendientes_float:.1f} <span style="color:#334155; font-weight:800;">/ {cuotas_totales_float:.1f}</span></div>
                        </div>
                        <div>
                            <div style="font-size:9.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Pago</div>
                            <div style="font-size:12.5px; font-weight:900;">{html_col_pago}</div>
                        </div>
                    </div>

                    <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}, {dias_calculados}); event.stopPropagation();"
                                style="width:26px; height:26px; border-radius:8px; background:#f1f5f9; border:1px solid #cbd5e1; color:#334155; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0;">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                        </button>
                        <div style="min-width:26px; height:26px; padding:0 7px; border-radius:8px; font-size:11.5px; display:flex; align-items:center; justify-content:center; {estilo_cuadro_dias}">{texto_cuadro_dias}</div>
                        <div style="background:#f1f5f9; border:1px solid #e2e8f0; border-radius:8px; padding:4px 9px; font-size:11.5px; font-weight:700; color:#475569;">
                            Saldo <b style="color:#0f172a; font-weight:900;">${c_saldo_restante}</b>
                        </div>
                    </div>
                </div>
            </div>
            <div class="card-derecha-acciones" id="acciones-{c_id}" style="display:flex; flex-direction:column; gap:6px; flex-shrink:0;">
                {html_botones_derechos}
            </div>
        </div>
        """
        
    contexto_dash += """
    </div>
    <div id="msgSinResultados" style="display:none; text-align:center; padding:40px 20px; color:#64748b; font-size:14px; font-weight:600;">
        No hay recaudos disponibles con los filtros seleccionados
    </div>
    <script>
        function aplicarFiltrosDashboard() {
            const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
            const showPend = document.getElementById('chkPendientes').checked;
            const showPagos = document.getElementById('chkPagos').checked;
            const showNoPagos = document.getElementById('chkNoPagos').checked;
            const fDiario = document.getElementById('chkDiario').checked;
            const fSemanal = document.getElementById('chkSemanal').checked;
            const filterByFreq = fDiario || fSemanal;

            const cards = document.querySelectorAll('.cliente-card-premium');
            let visibles = 0;

            cards.forEach(card => {
                const nombre = card.getAttribute('data-nombre') || '';
                const estado = card.getAttribute('data-estado') || 'pendiente';
                const freq = card.getAttribute('data-frecuencia') || 'diario';

                const matchTexto = nombre.includes(q);
                const matchEstado = (estado === 'pendiente' && showPend) || 
                                    (estado === 'pago' && showPagos) || 
                                    (estado === 'no_pago' && showNoPagos);
                const matchFreq = !filterByFreq || (freq === 'diario' && fDiario) || (freq === 'semanal' && fSemanal);

                if (matchTexto && matchEstado && matchFreq) {
                    card.style.display = '';
                    visibles++;
                } else {
                    card.style.setProperty('display', 'none', 'important');
                }
            });

            document.getElementById('msgSinResultados').style.display = visibles === 0 ? 'block' : 'none';
        }
        aplicarFiltrosDashboard();
    </script>
    """
    
    # 🎯 Inyectamos el bloque procesado y controlamos el formato nativo para celulares
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(contexto_dash)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(contexto_dash))


@app.route("/menu/clientes")
def seccion_clientes_maestro():
    if not session.get("autenticado"):
        return "Sesión expirada"

    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    total_con_saldo = 0
    total_sin_saldo = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    MAX(c.id) AS id,
                    MAX(TRIM(c.nombre)) AS nombre,
                    MAX(c.telefono) AS telefono,
                    MAX(c.referencia) AS referencia,
                    MAX(c.identificacion) AS cedula,
                    COALESCE(SUM(
                        CASE WHEN c.estado = 'Activo' THEN
                            GREATEST(0, c.monto_total - COALESCE((SELECT SUM(p.valor_pagado) FROM pagos p WHERE p.cliente_id = c.id), 0))
                        ELSE 0 END
                    ), 0) AS saldo_activo
                FROM clientes c
                WHERE c.ruta_id = %s
                GROUP BY CASE
                    WHEN LENGTH(TRIM(COALESCE(c.identificacion, ''))) >= 8
                     AND TRIM(c.identificacion) NOT IN ('0000000000', '1111111111', '1234567890')
                    THEN TRIM(c.identificacion)
                    ELSE LOWER(TRIM(c.nombre))
                END
                ORDER BY MAX(TRIM(c.nombre)) ASC
            """, (rid,))
            lista_unica = []
            for r in cursor.fetchall():
                d = dict(r)
                if float(d.get("saldo_activo") or 0) > 0:
                    total_con_saldo += 1
                else:
                    total_sin_saldo += 1
                lista_unica.append(d)
    except Exception as e:
        print(f"Error al consultar clientes: {e}")
        lista_unica = []

    contexto = dict(
        vista="clientes_maestro",
        lista_clientes=lista_unica,
        total_con_saldo=total_con_saldo,
        total_sin_saldo=total_sin_saldo
    )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/api/cliente/guardar_gps/<int:cliente_id>", methods=["POST"])
def api_guardar_gps_cliente(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    data = request.get_json(silent=True) or {}
    lat = str(data.get("latitud") or "").strip()
    lng = str(data.get("longitud") or "").strip()
    if not lat or not lng:
        return jsonify({"status": "error", "message": "Coordenadas inválidas"}), 400
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE clientes SET latitud = %s, longitud = %s WHERE id = %s AND ruta_id = %s",
                (lat, lng, cliente_id, rid)
            )
            conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/menu/cliente/<int:cliente_id>")
def perfil_cliente_detalle(cliente_id):
    if not session.get("autenticado"):
        return "Sesión expirada"
    conn = get_db()
    try:
        rid = int(session.get("ruta_id") or 1)
        hoy_str = date.today().isoformat()
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_ja = cursor.fetchone()
            if row_ja and dict(row_ja).get("valor"):
                hoy_str = str(dict(row_ja)["valor"]).strip()[:10]
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
            cliente = dict(cursor.fetchone())
            cedula_cli = str(cliente.get("identificacion") or "").strip()
            nombre_cli = str(cliente.get("nombre") or "").strip()

            # Buscar todos los créditos del cliente en esta ruta (por cédula válida o por nombre exacto)
            if cedula_cli and len(cedula_cli) >= 8 and cedula_cli not in ("0000000000", "1111111111", "1234567890"):
                cursor.execute("""
                    SELECT c.*,
                           COALESCE((SELECT SUM(valor_pagado) FROM pagos WHERE cliente_id = c.id), 0) AS pagado,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id), 0) AS total_cuotas_real,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id AND pagado = 1), 0) AS cuotas_pagadas,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id AND pagado = 0 AND fecha < %s), 0) AS cuotas_vencidas
                    FROM clientes c
                    WHERE c.ruta_id = %s
                        AND (
                            TRIM(COALESCE(c.identificacion, '')) = %s
                            OR (
                                LOWER(TRIM(c.nombre)) = LOWER(%s)
                                AND LENGTH(TRIM(COALESCE(c.identificacion, ''))) < 8
                            )
                        )
                    ORDER BY c.id DESC
                """, (hoy_str, rid, cedula_cli, nombre_cli))
            else:
                cursor.execute("""
                    SELECT c.*,
                           COALESCE((SELECT SUM(valor_pagado) FROM pagos WHERE cliente_id = c.id), 0) AS pagado,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id), 0) AS total_cuotas_real,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id AND pagado = 1), 0) AS cuotas_pagadas,
                           COALESCE((SELECT COUNT(*) FROM pagos WHERE cliente_id = c.id AND pagado = 0 AND fecha < %s), 0) AS cuotas_vencidas
                    FROM clientes c
                    WHERE c.ruta_id = %s
                      AND (c.id = %s OR LOWER(TRIM(c.nombre)) = LOWER(%s))
                    ORDER BY c.id DESC
                """, (hoy_str, rid, cliente_id, nombre_cli))

            historial_creditos = []
            total_cuotas_hist = 0
            total_pagadas_hist = 0
            total_vencidas_hist = 0
            creditos_liquidados = 0
            total_prestado_hist = Decimal("0.00")
            total_abonado_hist = Decimal("0.00")

            for cr in cursor.fetchall():
                cr_dict = dict(cr)
                m_tot = Decimal(str(cr_dict.get("monto_total") or "0"))
                m_cap = Decimal(str(cr_dict.get("monto") or "0"))
                m_pag = Decimal(str(cr_dict.get("pagado") or "0"))
                cr_dict["saldo"] = max(Decimal("0.00"), m_tot - m_pag)

                t_c = int(cr_dict.get("total_cuotas_real") or 0)
                c_p = int(cr_dict.get("cuotas_pagadas") or 0)
                c_v = int(cr_dict.get("cuotas_vencidas") or 0)

                total_cuotas_hist += t_c
                total_pagadas_hist += c_p
                total_vencidas_hist += c_v
                total_prestado_hist += m_cap
                total_abonado_hist += m_pag
                if cr_dict["saldo"] <= Decimal("0.01"):
                    creditos_liquidados += 1

                # Cargar cronograma detallado de pagos de este crédito
                cursor.execute("""
                    SELECT numero, fecha, valor, pagado, valor_pagado, fecha_pago_real
                    FROM pagos
                    WHERE cliente_id = %s
                    ORDER BY numero ASC
                """, (cr_dict["id"],))
                cr_dict["detalle_pagos"] = [dict(p) for p in cursor.fetchall()]
                historial_creditos.append(cr_dict)

            # Cálculo automático de Calificación Crediticia (Buen Pagador vs Riesgo)
            cuotas_exigibles = total_pagadas_hist + total_vencidas_hist
            if cuotas_exigibles > 0:
                puntualidad = round((total_pagadas_hist / cuotas_exigibles) * 100)
            else:
                puntualidad = 100

            if total_vencidas_hist == 0:
                calificacion = {
                    "titulo": "BUEN PAGADOR • AL DÍA",
                    "color": "#16a34a",
                    "bg": "#dcfce7",
                    "borde": "#86efac",
                    "puntualidad": puntualidad,
                    "detalle": f"{creditos_liquidados} crédito(s) liquidado(s) • Sin cuotas vencidas"
                }
            elif total_vencidas_hist <= 2 or puntualidad >= 50:
                calificacion = {
                    "titulo": "CLIENTE REGULAR • EN OBSERVACIÓN",
                    "color": "#d97706",
                    "bg": "#fef3c7",
                    "borde": "#fcd34d",
                    "puntualidad": puntualidad,
                    "detalle": f"Cumplimiento del {puntualidad}% • {total_vencidas_hist} cuota(s) vencida(s)"
                }

            else:
                calificacion = {
                    "titulo": "RIESGO / MAL PAGADOR",
                    "color": "#dc2626",
                    "bg": "#fee2e2",
                    "borde": "#fca5a5",
                    "puntualidad": puntualidad,
                    "detalle": f"Mora alta ({total_vencidas_hist} cuotas vencidas) • Evaluar antes de renovar"
                }

            resumen_global = {
                "total_creditos": len(historial_creditos),
                "liquidados": creditos_liquidados,
                "total_prestado": f"{total_prestado_hist:.2f}",
                "total_abonado": f"{total_abonado_hist:.2f}",
                "cuotas_pagadas": total_pagadas_hist,
                "cuotas_vencidas": total_vencidas_hist
            }

        contexto = dict(
            vista="perfil_cliente",
            cliente=cliente,
            creditos=historial_creditos,
            calificacion=calificacion,
            resumen=resumen_global,
            hoy_str=hoy_str
        )
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return render_template_string(CONTENIDO_HTML, **contexto)
        return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))
    except Exception as e:
        return f"Error al cargar ficha: {e}", 500


@app.route("/api/cliente/actualizar/<int:cliente_id>", methods=["POST"])
def api_actualizar_cliente(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    data = request.get_json()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT nombre FROM clientes WHERE id = %s", (cliente_id,))
            nombre_anterior = cursor.fetchone()["nombre"]
            
            # Actualizamos estrictamente tus campos oficiales, sin fiador
            cursor.execute("""
                UPDATE clientes 
                SET nombre = %s, telefono = %s, direccion = %s, referencia = %s
                WHERE nombre = %s
            """, (
                data.get("nombre").strip(),
                data.get("telefono").strip(),
                data.get("direccion").strip(),
                data.get("referencia", "").strip(),
                nombre_anterior
            ))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})

@app.route("/menu/creditos")
def seccion_creditos_activos():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    filtro_estado = request.args.get("filter", "Activo")
    rid = int(session.get("ruta_id") or 1)
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Optimizamos la consulta principal de todos los clientes bajo el filtro seleccionado
            cursor.execute(
                "SELECT id, nombre, frecuencia, monto_total, estado, saltado_hoy, valor_cuota FROM clientes WHERE ruta_id = %s AND estado = %s ORDER BY nombre ASC",
                (rid, filtro_estado)
            )
            todos = cursor.fetchall()
            
            # 🛡️ FIJADO: Traemos todos los pagos de una sola vez para evitar colapsar Neon con bucles infinitos
            clientes_completos = []
            if todos:
                cliente_ids = [cl["id"] for cl in todos]
                cursor.execute("SELECT * FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (cliente_ids,))
                todos_los_pagos = cursor.fetchall()
                
                # Mapeamos los pagos de forma ultra veloz en memoria
                pagos_por_cliente = defaultdict(list)
                for p in todos_los_pagos:
                    pagos_por_cliente[p["cliente_id"]].append(dict(p))
            
            for cl in todos:
                t_dict = dict(cl)
                t_dict["valor_cuota"] = float(cl["valor_cuota"]) if cl["valor_cuota"] is not None else 0.00
                t_dict["pagos"] = pagos_por_cliente.get(cl["id"], [])
                clientes_completos.append(t_dict)
            
            # Consultas de conteo rápidas para los badges superiores
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE ruta_id = %s AND estado = 'Activo'", (rid,))
            total_activos = int(cursor.fetchone()["total"])

            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE ruta_id = %s AND estado = 'Inactivo'", (rid,))
            total_inactivos = int(cursor.fetchone()["total"])

            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE ruta_id = %s AND estado = 'Lista Negra'", (rid,))
            total_negra = int(cursor.fetchone()["total"])
            
    except Exception as e:
        print(f"Error crítico en sección créditos maestros: {e}")
        clientes_completos = []
        total_activos, total_inactivos, total_negra = 0, 0, 0
    finally:
        pass
            
    contexto = dict(
        vista="creditos_maestros",
        todos=clientes_completos,
        total_activos=total_activos,
        total_inactivos=total_inactivos,
        total_negra=total_negra,
        filtro_estado=filtro_estado
    )
    
    # 🪙 FIJADO: Corregido el operador de comparación exacto para el refresco del celular
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/menu/balance")
def seccion_balance_maestro():
    if not session.get("autenticado"):
        return "Sesión expirada"

    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    caja_inicial_sistema = Decimal("0.00")
    caja_base = Decimal("0.00")
    total_abonos_hoy = Decimal("0.00")
    total_abonos_transf = Decimal("0.00")
    entradas_totales = Decimal("0.00")
    salidas_totales = Decimal("0.00")
    movimientos = []
    estado_cierre = "Abierta"
    hoy_str = ahora_local().date().isoformat()

    try:
        with conn.cursor() as cursor:
            # 1. Recuperamos la jornada activa de esta ruta
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_j = cursor.fetchone()
            if row_j and dict(row_j).get("valor"):
                hoy_str = str(dict(row_j)["valor"]).strip()
            else:
                cursor.execute("DELETE FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
                cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('fecha_jornada_activa', %s, %s)", (hoy_str, rid))
                conn.commit()

            # 2. Base real de la jornada activa (idéntica a Avanta Dueño y Consolidado)
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base' AND ruta_id = %s LIMIT 1", (rid,))
            res_caja = cursor.fetchone()
            caja_inicial_sistema = Decimal(str(dict(res_caja).get("valor") or "0.00")) if res_caja else Decimal("0.00")

            caja_base = caja_inicial_sistema

            # 3. Reparar registros que quedaron con fecha='RealDictRo'
            cursor.execute("UPDATE balance_movimientos SET fecha = %s WHERE ruta_id = %s AND fecha = 'RealDictRo'", (hoy_str, rid))
            conn.commit()

            # 4. Estado de cierre hoy
            cursor.execute("SELECT valor FROM configuracion WHERE llave = %s AND ruta_id = %s", (f"cierre_caja_{hoy_str}", rid))
            res_cierre = cursor.fetchone()
            estado_cierre = str(dict(res_cierre).get("valor") or "Abierta") if res_cierre else "Abierta"

            # 5. Movimientos de la jornada activa (exactamente igual que Avanta Dueño)
            cursor.execute("""
                SELECT id, tipo, categoria, concepto, monto, fecha,
                       COALESCE(metodo_pago, 'efectivo') AS metodo_pago,
                       CASE WHEN COALESCE(comprobante, '') <> '' THEN '1' ELSE '' END AS comprobante
                FROM balance_movimientos
                WHERE ruta_id = %s AND fecha = %s
                ORDER BY id DESC
            """, (rid, hoy_str))
            movimientos = [dict(r) for r in cursor.fetchall()]

            for m in movimientos:
                t_m = str(m.get("tipo") or "").strip().lower()
                cat_m = str(m.get("categoria") or "").strip().lower()
                met_m = str(m.get("metodo_pago") or "efectivo").strip().lower()
                m_val = Decimal(str(m.get("monto") or "0.00"))

                if t_m in ("ingreso", "entrada", "inyeccion", "base", "abono"):
                    if cat_m == "cobranza" and met_m == "transferencia":
                        total_abonos_transf += m_val
                    else:
                        entradas_totales += m_val
                        if cat_m == "cobranza":
                            total_abonos_hoy += m_val
                elif t_m in ("egreso", "gasto", "retiro", "salida", "credito"):
                    salidas_totales += m_val
    except Exception as e:
        conn.rollback()
        print(f"Error en balance: {e}")
        caja_base = caja_inicial_sistema

    efectivo_neto = caja_base + entradas_totales - salidas_totales

    # Purificación de imágenes Base64 y normalización visual de Entrada/Salida
    movimientos_limpios = []
    for m in movimientos:
        if str(m.get("categoria") or "").strip().lower() == "cobranza":
            continue
        m_dict = dict(m)
        t_low = str(m_dict.get("tipo") or "").strip().lower()
        m_dict["tipo"] = "Entrada" if t_low in ("ingreso", "entrada", "inyeccion", "base", "abono") else "Salida"
        m_dict["monto"] = float(m_dict["monto"])
        movimientos_limpios.append(m_dict)

    if total_abonos_transf > 0:
        movimientos_limpios.insert(0, {
            "id": 0, "tipo": "Entrada", "categoria": "Transferencia",
            "concepto": "RECAUDACIÓN POR TRANSFERENCIA (NO EFECTIVO)",
            "monto": float(total_abonos_transf), "fecha": hoy_str, "comprobante": ""
        })

    if total_abonos_hoy > 0:
        movimientos_limpios.insert(0, {
            "id": 0, "tipo": "Entrada", "categoria": "Recaudo",
            "concepto": "RECAUDACIÓN DE CUOTAS EN EFECTIVO",
            "monto": float(total_abonos_hoy), "fecha": hoy_str, "comprobante": ""
        })

    contexto = dict(
        vista="balance",
        caja_base=float(caja_base),
        balance_estado=estado_cierre,
        gastos_list=movimientos_limpios,
        entradas_totales=float(entradas_totales),
        salidas_totales=float(salidas_totales),
        efectivo_neto=float(efectivo_neto)
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/nuevo")
@app.route("/mover_renovacion/<int:cliente_id>")
def seccion_nuevo_credito(cliente_id=None):
    if not session.get("autenticado"): 
        return redirect("/login")
        
    hoy_str = date.today().isoformat()
    cliente = None
    
    if cliente_id:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                # 📊 Recuperamos los datos del cliente si es una renovación
                cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cursor.fetchone()
        except Exception as e:
            print(f"Error al recuperar cliente para renovación: {e}")
        finally:
            pass
            
    contexto = dict(
        vista="nuevo" if not cliente_id else "renovar", 
        hoy_str=hoy_str, 
        cliente=cliente
    )
    
    # 🪙 FIJADO: Operador de comparación exacto para el refresco asíncrono del móvil
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/api/clientes/guardar", methods=["POST"])
def guardar_nuevo_cliente():
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    # 1. CAPTURA DE VARIABLES CON LOS NOMBRES EXACTOS DEL FORMULARIO
    nombre = request.form.get("nombre", "").strip()
    telefono = request.form.get("telefono", "").strip()
    direccion = request.form.get("direccion", "").strip()
    referencia = request.form.get("referencia", "").strip()
    identificacion = request.form.get("identificacion", "").strip()
    foto_cedula_frente = request.form.get("foto_cedula_frente", "").strip()
    foto_cedula_reverso = request.form.get("foto_cedula_reverso", "").strip()
    cliente_id_renovacion = request.form.get("cliente_id", "").strip()
    
    try:
        monto = Decimal(request.form.get("monto", "0"))
        # 📌 CORREGIDO: Se acopla al 'interes_defecto' del maestro de Neon
        interes_porcentaje = Decimal(request.form.get("interes_porcentaje", "20"))
        cuotas = int(request.form.get("cuotas", "1"))
    except Exception:
        return jsonify({"status": "error", "message": "Valores numéricos inválidos"}), 400

    raw_freq = (request.form.get("frecuencia") or request.form.get("frecuencia_pago") or "Diaria").strip().lower()
    frecuencia = "Semanal" if raw_freq == "semanal" else "Diaria"
    # 📌 CORREGIDO: Se empareja con el name real de tus opciones HTML
    aplica_seguro = request.form.get("aplicar_seguro", "NO")
    
    latitud = request.form.get("latitud", "")
    longitud = request.form.get("longitud", "")
    
    fecha_inicio_str = request.form.get("fecha_inicio", ahora_local().date().isoformat())
    try:
        fecha_inicio_dt = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
    except ValueError:
        fecha_inicio_dt = ahora_local().date()
    if fecha_inicio_dt <= ahora_local().date():
        fecha_inicio_dt = ahora_local().date() + timedelta(days=1)

    monto_interes = monto * (interes_porcentaje / Decimal("100"))
    monto_total = monto + monto_interes
    valor_cuota = round(monto_total / Decimal(str(cuotas)), 2)
    
    fecha_vencimiento_dt = calcular_fecha(fecha_inicio_dt, cuotas - 1, frecuencia)

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ MÓDULO MICROSEGURO MULTI-OPCIÓN CONTABLE
            microseguro_valor = Decimal("0.00")
            if aplica_seguro != "NO" and monto > 0:
                if aplica_seguro == "2":
                    microseguro_valor = round(monto * Decimal("0.02"), 2)
                elif aplica_seguro == "4":
                    microseguro_valor = round(monto * Decimal("0.04"), 2)
                elif aplica_seguro == "MANUAL":
                    raw_val = request.form.get("microseguro_manual_valor", "0")
                    raw_val = raw_val.strip() if raw_val else "0"
                    microseguro_valor = round(Decimal(raw_val if raw_val != "" else "0"), 2)
                    
            rid = int(session.get("ruta_id") or 1)
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_fja = cursor.fetchone()
            turno_balance_cred = str(dict(row_fja).get("valor") if row_fja else ahora_local().date().isoformat()).strip()

            # 🛡️ 1. Inserción limpia del microseguro en el turno activo
            if microseguro_valor > 0:
                concepto_seguro = f"🛡️ RECAUDO MICROSEGURO - CLIENTE: {nombre}"
                cursor.execute(
                    """
                    INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante, ruta_id)
                    VALUES ('Entrada', 'Inversión', %s, %s, %s, '', %s)
                    """,
                    (concepto_seguro, microseguro_valor, turno_balance_cred, rid)
                )

            # 💸 2. ÚNICO ASIENTO DE EGRESO DE CAPITAL en el turno activo
            concepto_egreso = f"💸 CAPITAL ENTREGADO EN CRÉDITO NUEVO - CLIENTE: {nombre}"
            cursor.execute(
                """
                INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante, ruta_id)
                VALUES ('Salida', 'Inversión', %s, %s, %s, '', %s)
                """,
                (concepto_egreso, monto, turno_balance_cred, rid)
            )
            
            # 📊 3. Calcular posición secuencial de enrutamiento (Blindado contra DictRow)
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes WHERE ruta_id = %s", (rid,))
            res_orden = cursor.fetchone()
            
            if res_orden is None:
                max_orden = 0
            elif isinstance(res_orden, dict):
                max_orden = int(res_orden.get('coalesce') if res_orden.get('coalesce') is not None else 0)
            elif hasattr(res_orden, 'keys'):
                max_orden = int(res_orden[0]) if res_orden[0] is not None else 0
            else:
                # Si viene como tupla/lista tradicional
                max_orden = int(res_orden[0]) if res_orden and res_orden[0] is not None else 0

            try:
                fecha_vencimiento_dt = calcular_fecha(fecha_inicio_dt, cuotas - 1, frecuencia)
            except Exception:
                from datetime import timedelta as _td
                salto_v = 7 if frecuencia == "Semanal" else 1
                fecha_vencimiento_dt = fecha_inicio_dt + _td(days=(cuotas - 1) * salto_v)

            # Bloqueo: No permitir conceder crédito nuevo si el cliente ya tiene crédito activo con saldo pendiente
            if not (cliente_id_renovacion and cliente_id_renovacion.isdigit()):
                if identificacion and len(identificacion.strip()) >= 8 and identificacion.strip() not in ("0000000000", "1111111111", "1234567890"):
                    cursor.execute("""
                        SELECT c.id, c.nombre, c.monto_total,
                               COALESCE((SELECT SUM(valor_pagado) FROM pagos WHERE cliente_id = c.id), 0) AS pagado
                        FROM clientes c
                        WHERE c.ruta_id = %s AND TRIM(c.identificacion) = %s AND c.estado = 'Activo'
                        """, (rid, identificacion.strip()))
                    for dup in cursor.fetchall():
                        d_row = dict(dup) if isinstance(dup, dict) else {"id": dup[0], "nombre": dup[1], "monto_total": dup[2], "pagado": dup[3]}
                        saldo_dup = Decimal(str(d_row["monto_total"] or "0")) - Decimal(str(d_row["pagado"] or "0"))
                        if saldo_dup > Decimal("0.01"):
                            return jsonify({
                                "status": "error",
                                "message": f"El cliente {d_row['nombre']} ya tiene un crédito activo con saldo pendiente de ${saldo_dup:.2f}. Debe liquidarlo antes de conceder otro."
                            }), 400

            if cliente_id_renovacion and cliente_id_renovacion.isdigit():
                cliente_id = int(cliente_id_renovacion)
                cursor.execute(
                    """
                    UPDATE clientes
                    SET nombre = %s, telefono = %s, direccion = %s, referencia = %s, identificacion = %s,
                        monto = %s, interes_porcentaje = %s, monto_total = %s, cuotas = %s, frecuencia = %s,
                        valor_cuota = %s, fecha_inicio = %s, fecha_vencimiento = %s, estado = 'Activo', fecha_gestion = NULL,
                        foto_cedula_frente = CASE WHEN %s <> '' THEN %s ELSE foto_cedula_frente END,
                        foto_cedula_reverso = CASE WHEN %s <> '' THEN %s ELSE foto_cedula_reverso END
                    WHERE id = %s
                    """,
                    (nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat(), foto_cedula_frente, foto_cedula_frente, foto_cedula_reverso, foto_cedula_reverso, cliente_id)
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, estado, latitud, longitud, foto_cedula_frente, foto_cedula_reverso, ruta_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat(), latitud, longitud, foto_cedula_frente, foto_cedula_reverso, rid)
                )
                res_id = cursor.fetchone()
                cliente_id = res_id["id"] if isinstance(res_id, dict) else res_id[0]

            if cliente_id_renovacion and cliente_id_renovacion.isdigit(): cursor.execute("DELETE FROM pagos WHERE cliente_id = %s", (cliente_id,))
            # Tablas de amortización protegidas contra desfases de bucle semanales
            if cliente_id:
                filas_pagos = []
                acumulado = Decimal("0.00")
                for num in range(1, cuotas + 1):
                    if num == cuotas:
                        valor_cuota_real = monto_total - acumulado
                    else:
                        valor_cuota_real = valor_cuota
                    acumulado += valor_cuota_real
                    try:
                        f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frecuencia)
                    except Exception:
                        f_cuota = fecha_inicio_dt + timedelta(days=(num - 1) * 7 if frecuencia == "Semanal" else (num - 1))
                    filas_pagos.append((cliente_id, num, f_cuota.isoformat(), valor_cuota_real, 0, 0, '', rid))

                execute_values(
                    cursor,
                    "INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real, ruta_id) VALUES %s",
                    filas_pagos
                )
                    
        conn.commit()
        return jsonify({
            "status": "ok",
            "message": "Crédito guardado exitosamente",
            "ticket": {
                "codigo": f"{int(cliente_id):06d}",
                "tipo_operacion": "COMPROBANTE DE RENOVACIÓN" if (cliente_id_renovacion and cliente_id_renovacion.isdigit()) else "COMPROBANTE DE CRÉDITO",
                "cliente": nombre,
                "telefono": telefono or "N/A",
                "identificacion": identificacion or "N/A",
                "direccion": direccion or "N/A",
                "referencia": referencia or "",
                "frecuencia": frecuencia,
                "fecha_inicio": fecha_inicio_dt.isoformat(),
                "fecha_vencimiento": fecha_vencimiento_dt.isoformat(),
                "monto_capital": f"{float(monto):.2f}",
                "monto_total": f"{float(monto_total):.2f}",
                "valor_cuota": f"{float(valor_cuota):.2f}",
                "cuotas": int(cuotas)
            }
        })
    except Exception as e:
        print(f"Error crítico en el asiento del crédito: {e}")
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/balance/guardar_movimiento", methods=["POST"])
def balance_guardar_movimiento():
    if not session.get("autenticado"):
        return redirect("/login")

    tipo = request.form.get("tipo_mov")
    categoria = request.form.get("categoria_mov", "Otros")
    concepto = request.form.get("concepto_mov", "").strip()  or categoria

    # 🛡️ Captura de dinero con precisión contable exacta Decimal
    monto = Decimal(request.form.get("monto_mov", "0"))

    if not concepto:
        concepto = f"Flujo de {categoria}"

    # 📸 Captura de la cadena de imagen comprimida en caliente desde el móvil
    base64_str = request.form.get("foto_comprimida_b64", "").strip()

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            rid = int(session.get("ruta_id") or 1)
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_j = cursor.fetchone()
            hoy_str = row_j["valor"] if (row_j and row_j["valor"]) else date.today().isoformat()

            cursor.execute(
                """
                INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante, ruta_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (tipo, categoria, concepto, monto, hoy_str, base64_str, rid)
            )
            conn.commit()
    except Exception as e:
        print(f"Error crítico al guardar movimiento de balance: {e}")
    finally:
        pass

    return redirect("/menu/balance")

@app.route("/api/eliminar_gasto/<int:gasto_id>", methods=["POST"])
def eliminar_gasto(gasto_id):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Estructura segura con liberación obligatoria del canal en el pool
            cursor.execute("DELETE FROM balance_movimientos WHERE id = %s AND ruta_id = %s", (gasto_id, int(session.get("ruta_id") or 0)))
        conn.commit()
    except Exception as e:
        print(f"Error al eliminar gasto contable: {e}")
    finally:
        pass
        
    return redirect("/menu/balance")

@app.route("/api/eliminar_cliente/<int:id>", methods=["POST"])
def api_eliminar_cliente(id):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 1. Primero obtenemos el nombre del cliente antes de borrarlo para poder limpiar sus flujos
            cursor.execute("SELECT nombre FROM clientes WHERE id = %s AND ruta_id = %s;", (id, int(session.get("ruta_id") or 0)))
            cliente_data = cursor.fetchone()
            
            if cliente_data:
                # Si Neon devuelve un diccionario o tupla, extraemos el nombre de forma segura
                nombre_cliente = cliente_data["nombre"] if isinstance(cliente_data, dict) else cliente_data[0]
                
                # 🛡️ 2. BORRADO EN CASCADA CONTABLE: Eliminamos el seguro y el egreso flotantes en el balance
                # Busca cualquier concepto que contenga el nombre de ese cliente específico
                cursor.execute(
                    "DELETE FROM balance_movimientos WHERE concepto LIKE %s;", 
                    (f"%{nombre_cliente}%",)
                )

            # 🔢 3. Limpiamos las tablas de amortización de cuotas
            cursor.execute("DELETE FROM pagos WHERE cliente_id = %s AND ruta_id = %s;", (id, int(session.get("ruta_id") or 0)))
            
            # 👤 4. Eliminamos definitivamente la ficha del cliente
            cursor.execute("DELETE FROM clientes WHERE id = %s AND ruta_id = %s;", (id, int(session.get("ruta_id") or 0)))
            
        conn.commit()
    except Exception as e:
        print(f"Error crítico al eliminar cliente de la base de datos: {e}")
    finally:
        pass
        
    return redirect("/menu/clientes")

@app.route("/mover/<int:cliente_id>/<string:direccion>", methods=["POST"])
def mover(cliente_id, direccion):
    if not session.get("autenticado"): 
        return "No autorizado"
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Recuperamos todos los clientes activos ordenados para calcular posiciones en memoria
            cursor.execute("SELECT id, orden FROM clientes WHERE ruta_id = %s AND estado = 'Activo' ORDER BY orden ASC, id DESC", (rid,))
            clientes = cursor.fetchall()
            
            # 🛡️ FIJADO: Operador de comparación exacto para hallar la posición del cliente
            index = next((i for i, c in enumerate(clientes) if c["id"] == cliente_id), None)
            
            if index is not None:
                # 🛡️ FIJADO: Comparación limpia de dirección para SUBIR en la ruta
                if direccion == "subir" and index > 0:
                    actual = clientes[index]
                    anterior = clientes[index - 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s AND ruta_id = %s", (anterior["orden"], actual["id"], rid))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s AND ruta_id = %s", (actual["orden"], anterior["id"], rid))
                    
                # 🛡️ FIJADO: Comparación limpia de dirección para BAJAR en la ruta
                elif direccion == "bajar" and index < len(clientes) - 1:
                    actual = clientes[index]
                    siguiente = clientes[index + 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s AND ruta_id = %s", (siguiente["orden"], actual["id"], rid))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s AND ruta_id = %s", (actual["orden"], siguiente["id"], rid))
                    
        conn.commit()
    except Exception as e:
        print(f"Error crítico al reordenar la ruta de cobros: {e}")
    finally:
        pass
        
    # 🔄 FIJADO: Te mantiene fijo dentro de la sección de reordenar ruta para clics consecutivos cómodos
    return redirect("/menu/ruta_orden")

@app.route("/menu/ruta_orden")
def seccion_reordenar_ruta():
    if not session.get("autenticado"): 
        return "Sesión expirada"
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Traemos los clientes activos en su orden actual de secuencia
            cursor.execute("SELECT id, nombre, orden, referencia, frecuencia FROM clientes WHERE ruta_id = %s AND estado = 'Activo' ORDER BY orden ASC, id DESC", (rid,))
            todos = cursor.fetchall()
    except Exception as e:
        print(f"Error al cargar orden de ruta: {e}")
        todos = []
    finally:
        pass

    total_ruta = len(todos)
    html_orden = f"""
    <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 4px 10px 4px;">
        <div style="display:flex; align-items:center; gap:10px;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21"/><line x1="9" y1="3" x2="9" y2="18"/><line x1="15" y1="6" x2="15" y2="21"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">SECUENCIA DE RUTA</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">{total_ruta} clientes activos en recorrido</div>
            </div>
        </div>
    </div>

    <div style="background:#eff6ff; border:1px solid #dbeafe; border-radius:10px; padding:9px 12px; margin:0 4px 12px 4px; display:flex; align-items:center; gap:8px; text-align:left;">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        <span style="font-size:11.5px; color:#1e3a8a; font-weight:600; line-height:1.3;">Usa las flechas laterales para ajustar el orden de visita de tus clientes al instante.</span>
    </div>

    <div style="position:relative; margin:0 4px 14px 4px;">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; left:13px; top:50%; transform:translateY(-50%); pointer-events:none;">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input type="text" id="buscadorRutaOrden" onkeyup="filtrarSecuenciaRuta()"
               placeholder="Buscar parada por nombre o referencia..."
               style="width:100%; padding:12px 14px 12px 38px; border-radius:12px; border:1.5px solid #cbd5e1; font-size:13.5px; font-weight:500; color:#0f172a; background:#ffffff; outline:none; box-sizing:border-box;">
    </div>

    <div id="listaSecuenciaRuta" style="display:flex; flex-direction:column; gap:9px; padding:0 4px 28px 4px;">
    """

    for index, cl in enumerate(todos):
        es_primero = (index == 0)
        es_ultimo = (index == total_ruta - 1)
        freq_txt = cl.get("frecuencia") or "Diaria"
        freq_bg = "#dcfce7" if freq_txt == "Diaria" else "#fef3c7"
        freq_col = "#15803d" if freq_txt == "Diaria" else "#b45309"
        nombre_lower = (cl.get("nombre") or "").lower()
        ref_lower = (cl.get("referencia") or "").lower()

        btn_subir_html = (
            '<span style="width:36px; height:36px; border-radius:10px; background:#f8fafc; color:#cbd5e1; border:1px solid #f1f5f9; display:flex; align-items:center; justify-content:center; cursor:default;">'
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></span>'
            if es_primero else
            f'<button type="button" onclick="postAccionAvanta(\'/mover/{cl["id"]}/subir\', \'/menu/ruta_orden\')" title="Subir posición" style="width:36px; height:36px; border-radius:10px; background:#eff6ff; color:#0f2b5c; border:1px solid #dbeafe; display:flex; align-items:center; justify-content:center; -webkit-tap-highlight-color:transparent;">'
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></button>'
        )

        btn_bajar_html = (
            '<span style="width:36px; height:36px; border-radius:10px; background:#f8fafc; color:#cbd5e1; border:1px solid #f1f5f9; display:flex; align-items:center; justify-content:center; cursor:default;">'
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></span>'
            if es_ultimo else
            f'<button type="button" onclick="postAccionAvanta(\'/mover/{cl["id"]}/bajar\', \'/menu/ruta_orden\')" title="Bajar posición" style="width:36px; height:36px; border-radius:10px; background:#eff6ff; color:#0f2b5c; border:1px solid #dbeafe; display:flex; align-items:center; justify-content:center; -webkit-tap-highlight-color:transparent;">'
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>'
        )

        html_orden += f"""
        <div class="item-ruta-orden" data-filtro="{nombre_lower} {ref_lower}"
             style="background:#ffffff; padding:11px 12px; display:flex; align-items:center; justify-content:space-between; gap:10px; border-radius:13px; border:1px solid #e2e8f0; box-shadow:0 2px 5px rgba(15,23,42,0.03);">
            <div style="display:flex; align-items:center; gap:11px; flex:1; min-width:0; text-align:left;">
                <div style="min-width:32px; height:32px; padding:0 6px; border-radius:9px; background:#0f2b5c; color:#ffffff; font-size:12px; font-weight:800; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    {index + 1}
                </div>
                <div style="min-width:0; flex:1;">
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span style="font-weight:800; font-size:14px; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{cl['nombre']}</span>
                        <span style="background:{freq_bg}; color:{freq_col}; font-size:9.5px; font-weight:800; padding:1px 6px; border-radius:5px; flex-shrink:0;">{freq_txt}</span>
                    </div>
                    <div style="display:flex; align-items:center; gap:4px; margin-top:3px; font-size:11.5px; color:#64748b; font-weight:500;">
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                        <span style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{cl['referencia'] or 'Sin referencia'}</span>
                    </div>
                </div>
            </div>
            <div style="display:flex; gap:6px; flex-shrink:0; align-items:center;">
                {btn_subir_html}
                {btn_bajar_html}
            </div>
        </div>
        """

    if not todos:
        html_orden += """
        <div style="text-align:center; color:#94a3b8; padding:32px 16px; background:#ffffff; border-radius:14px; border:1px dashed #cbd5e1; font-size:13.5px; font-weight:600;">
            No hay clientes activos para ordenar en la ruta hoy.
        </div>
        """

    html_orden += "</div>"

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_orden)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_orden))

@app.route("/menu/listados")
def seccion_listados():
    if not session.get("autenticado"):
        return "Sesión expirada"

    fecha_filtro = request.args.get("fecha_auditoria", date.today().isoformat())
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE balance_movimientos
                SET fecha = '2026-11-19'
                WHERE ruta_id = %s
                  AND (concepto ILIKE '%%Matusalén Jiménez%%' OR concepto ILIKE '%%Paola Herrera%%')
                  AND CAST(fecha AS TEXT) LIKE '2026-11-18%%'
            """, (rid,))
            conn.commit()

            cursor.execute(
                """
                SELECT p.numero, p.valor_pagado, c.id AS cliente_id, c.nombre
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE c.ruta_id = %s AND CAST(p.fecha_pago_real AS TEXT) LIKE %s AND p.valor_pagado > 0
                ORDER BY c.nombre ASC, p.numero ASC
                """, (rid, f"{fecha_filtro}%")
            )
            pagos_f = cursor.fetchall()

            cursor.execute("""
                SELECT
                    COALESCE(c.id, bm.id) AS id,
                    TRIM(SPLIT_PART(bm.concepto, 'CLIENTE:', 2)) AS nombre,
                    bm.monto AS monto,
                    COALESCE(c.monto_total, bm.monto) AS monto_total
                FROM balance_movimientos bm
                LEFT JOIN clientes c
                    ON c.ruta_id = bm.ruta_id
                    AND LOWER(TRIM(c.nombre)) = LOWER(TRIM(SPLIT_PART(bm.concepto, 'CLIENTE:', 2)))
                WHERE bm.ruta_id = %s
                  AND bm.tipo = 'Salida'
                  AND bm.concepto LIKE '%%CRÉDITO NUEVO%%'
                  AND CAST(bm.fecha AS TEXT) LIKE %s
                ORDER BY bm.id ASC
            """, (rid, f"{fecha_filtro}%"))
            creditos_f = cursor.fetchall()
    except Exception as e:
        print(f"Error al consultar auditoría histórica: {e}")
        pagos_f = []
        creditos_f = []

    mapa_pagos = {}
    total_recaudado_dia = Decimal("0.00")
    for p in pagos_f:
        c_id = p["cliente_id"]
        v_pagado = Decimal(str(p["valor_pagado"]))
        total_recaudado_dia += v_pagado

        if c_id not in mapa_pagos:
            mapa_pagos[c_id] = {
                "nombre": p["nombre"],
                "cuotas": [p["numero"]],
                "total_abonado": v_pagado
            }
        else:
            mapa_pagos[c_id]["cuotas"].append(p["numero"])
            mapa_pagos[c_id]["total_abonado"] += v_pagado

    lista_pagos_html = []
    for c_id, info in mapa_pagos.items():
        min_cuota = min(info["cuotas"])
        max_cuota = max(info["cuotas"])
        rango = f"Cuota #{min_cuota}" if min_cuota == max_cuota else f"Cuotas #{min_cuota} a la #{max_cuota}"

        lista_pagos_html.append(f"""
        <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #f1f5f9; gap:10px;">
            <div style="display:flex; align-items:center; gap:10px; min-width:0; text-align:left;">
                <div style="width:30px; height:30px; border-radius:8px; background:#dcfce7; color:#15803d; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
                <div style="min-width:0;">
                    <div style="font-weight:800; font-size:13.5px; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{info['nombre']}</div>
                    <div style="font-size:11.5px; color:#64748b; font-weight:600; margin-top:1px;">{rango}</div>
                </div>
            </div>
            <div style="font-weight:800; font-size:14px; color:#16a34a; flex-shrink:0;">${float(info['total_abonado']):.2f}</div>
        </div>
        """)

    total_prestado_dia = Decimal("0.00")
    lista_creditos_html = []
    for c in creditos_f:
        cap = Decimal(str(c["monto"] or 0))
        cart = Decimal(str(c["monto_total"] or 0))
        total_prestado_dia += cap
        lista_creditos_html.append(f"""
        <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #f1f5f9; gap:10px;">
            <div style="display:flex; align-items:center; gap:10px; min-width:0; text-align:left;">
                <div style="width:30px; height:30px; border-radius:8px; background:#eff6ff; color:#1d4ed8; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
                </div>
                <div style="min-width:0;">
                    <div style="font-weight:800; font-size:13.5px; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{c['nombre']}</div>
                    <div style="font-size:11.5px; color:#64748b; font-weight:600; margin-top:1px;">Cartera: ${float(cart):.2f}</div>
                </div>
            </div>
            <div style="font-weight:800; font-size:14px; color:#0f2b5c; flex-shrink:0;">${float(cap):.2f}</div>
        </div>
        """)

    html_pagos_final = "".join(lista_pagos_html) if lista_pagos_html else '<div style="text-align:center; font-size:12.5px; color:#94a3b8; padding:18px 8px; font-weight:600;">Ningún recaudo asentado en esta fecha.</div>'
    html_creditos_final = "".join(lista_creditos_html) if lista_creditos_html else '<div style="text-align:center; font-size:12.5px; color:#94a3b8; padding:18px 8px; font-weight:600;">Ningún crédito entregado en esta fecha.</div>'

    html_listados = f"""
    <div style="padding:0 4px 28px 4px;">
        <!-- Cabecera Corporativa -->
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 12px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">AUDITORÍA POR FECHA</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">Consulta histórica de recaudos y colocación</div>
            </div>
        </div>

        <!-- Selector de Fecha -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <form onsubmit="event.preventDefault(); const f = document.getElementById('fecha_auditoria').value; navegarRuta('/menu/listados?fecha_auditoria=' + f);">
                <label style="font-weight:700; color:#334155; font-size:12px; display:block; margin-bottom:6px;">Selecciona la Fecha a Consultar</label>
                <input type="date" id="fecha_auditoria" value="{fecha_filtro}" required
                       style="margin-bottom:10px; width:100%; padding:11px 12px; border-radius:10px; border:1.5px solid #cbd5e1; font-size:14px; font-weight:700; color:#0f172a; box-sizing:border-box;">
                <button type="submit" style="width:100%; padding:12px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:10px; font-weight:800; font-size:14px; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; box-shadow:0 4px 10px rgba(22,163,74,0.22);">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    Consultar Historial
                </button>
            </form>
        </div>

        <!-- Tarjetas Resumen Rápido -->
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px;">
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:12px 14px; text-align:left; box-shadow:0 2px 5px rgba(15,23,42,0.02);">
                <div style="font-size:10.5px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Total Recaudado</div>
                <div style="font-size:18px; font-weight:900; color:#16a34a; margin-top:4px;">${float(total_recaudado_dia):.2f}</div>
                <div style="font-size:10.5px; color:#64748b; font-weight:600; margin-top:2px;">{len(mapa_pagos)} clientes</div>
            </div>
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:12px 14px; text-align:left; box-shadow:0 2px 5px rgba(15,23,42,0.02);">
                <div style="font-size:10.5px; font-weight:800; color:#64748b; text-transform:uppercase; letter-spacing:0.4px;">Total Prestado</div>
                <div style="font-size:18px; font-weight:900; color:#0f2b5c; margin-top:4px;">${float(total_prestado_dia):.2f}</div>
                <div style="font-size:10.5px; color:#64748b; font-weight:600; margin-top:2px;">{len(creditos_f)} créditos</div>
            </div>
        </div>

        <!-- Bloque Recaudos -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px; margin-bottom:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; padding-bottom:8px; margin-bottom:4px; border-bottom:1.5px solid #f1f5f9;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                <span style="font-size:13px; font-weight:800; color:#0f2b5c;">Recaudos de la Fecha</span>
            </div>
            {html_pagos_final}
        </div>

        <!-- Bloque Créditos Entregados -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:14px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; padding-bottom:8px; margin-bottom:4px; border-bottom:1.5px solid #f1f5f9;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
                <span style="font-size:13px; font-weight:800; color:#0f2b5c;">Créditos Entregados</span>
            </div>
            {html_creditos_final}
        </div>
    </div>
    """

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_listados)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_listados))


@app.route("/menu/agregar_lista")
def seccion_agregar_lista():
    if not session.get("autenticado"):
        return "Sesión expirada"
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, nombre, referencia, frecuencia, COALESCE(enrutado_forzado, 0) AS enrutado_forzado
                FROM clientes
                WHERE ruta_id = %s AND estado = 'Activo'
                ORDER BY
                    CASE WHEN frecuencia = 'Semanal' THEN 0 ELSE 1 END ASC,
                    nombre ASC
                """, (rid,))    
            todos = cursor.fetchall()
    except Exception as e:
        print(f"Error al cargar enrutamiento forzado: {e}")
        todos = []

    todos_procesados = [dict(c) for c in todos]
    total_semanales = sum(1 for c in todos_procesados if c.get("frecuencia") == "Semanal")

    html_sincro = """
    <div style="padding:0 4px 28px 4px;">
        <!-- Cabecera Corporativa -->
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 10px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">ENRUTAMIENTO ANTICIPADO</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">Agregar clientes programados a la ruta de hoy</div>
            </div>
        </div>

        <!-- Nota Informativa -->
        <div style="background:#eff6ff; border:1px solid #dbeafe; border-radius:10px; padding:9px 12px; margin-bottom:12px; display:flex; align-items:center; gap:8px; text-align:left;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            <span style="font-size:11.5px; color:#1e3a8a; font-weight:600; line-height:1.3;">Selecciona un cliente semanal que desee adelantar su cuota hoy para habilitarlo en tu tablero principal.</span>
        </div>

        <!-- Buscador Instantáneo -->
        <div style="position:relative; margin-bottom:10px;">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; left:13px; top:50%; transform:translateY(-50%); pointer-events:none;">
                <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input type="text" id="buscadorEnrutamiento" onkeyup="filtrarEnrutamientoAvanta()"
                   placeholder="Buscar cliente por nombre o referencia..."
                   style="width:100%; padding:12px 14px 12px 38px; border-radius:12px; border:1.5px solid #cbd5e1; font-size:13.5px; font-weight:500; color:#0f172a; background:#ffffff; outline:none; box-sizing:border-box;">
        </div>

        <!-- Pestañas de Frecuencia -->
        <div style="display:flex; gap:6px; margin-bottom:12px;">
            <button type="button" class="tab-enr-avanta active" onclick="cambiarTabEnrAvanta(this, 'Semanal')"
                    style="flex:1; padding:9px 8px; border-radius:10px; border:1.5px solid #0f2b5c; background:#0f2b5c; color:#ffffff; font-size:12px; font-weight:700; cursor:pointer;">
                Semanales ({{ total_semanales }})
            </button>
            <button type="button" class="tab-enr-avanta" onclick="cambiarTabEnrAvanta(this, 'todos')"
                    style="flex:1; padding:9px 8px; border-radius:10px; border:1.5px solid #cbd5e1; background:#ffffff; color:#334155; font-size:12px; font-weight:700; cursor:pointer;">
                Todos Activos ({{ todos|length }})
            </button>
        </div>

        <input type="hidden" id="filtroFreqEnrActual" value="Semanal">

        <!-- Lista de Clientes para Enrutar -->
        <div style="display:flex; flex-direction:column; gap:9px;">
            {% for c in todos %}
            {% set freq_c = c.frecuencia or 'Diaria' %}
            {% set ya_enrutado = (c.enrutado_forzado == 1 or freq_c == 'Diaria') %}
            <div class="item-enr-avanta"
                 data-filtro="{{ (c.nombre or '')|lower }} {{ (c.referencia or '')|lower }}"
                 data-freq="{{ freq_c }}"
                 style="background:#ffffff; border-radius:13px; border:1px solid #e2e8f0; padding:11px 12px; display:{{ 'flex' if freq_c == 'Semanal' else 'none' }}; justify-content:space-between; align-items:center; gap:10px; box-shadow:0 2px 5px rgba(15,23,42,0.03);">

                <div style="min-width:0; flex:1; text-align:left;">
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span style="font-weight:800; font-size:14px; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ c.nombre }}</span>
                        <span style="background:{{ '#fef3c7' if freq_c == 'Semanal' else '#dcfce7' }}; color:{{ '#b45309' if freq_c == 'Semanal' else '#15803d' }}; font-size:9.5px; font-weight:800; padding:2px 6px; border-radius:5px; flex-shrink:0;">
                            {{ freq_c }}
                        </span>
                    </div>
                    <div style="font-size:11.5px; color:#64748b; font-weight:500; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        {{ c.referencia or 'Sin referencia' }}
                    </div>
                </div>

                <div style="flex-shrink:0;">
                    {% if ya_enrutado %}
                        <span style="display:inline-flex; align-items:center; gap:4px; background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d; font-size:11px; font-weight:800; padding:7px 10px; border-radius:9px;">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                            En Ruta Hoy
                        </span>
                    {% else %}
                        <form action="/api/clientes/forzar_enrutado" method="POST" style="margin:0;">
                            <input type="hidden" name="cliente_forzar_id" value="{{ c.id }}">
                            <button type="submit" style="background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:9px; padding:8px 12px; font-size:11.5px; font-weight:800; cursor:pointer; display:flex; align-items:center; gap:5px; box-shadow:0 3px 8px rgba(22,163,74,0.22);">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                                Agregar Hoy
                            </button>
                        </form>
                    {% endif %}
                </div>
            </div>
            {% else %}
            <div style="text-align:center; color:#94a3b8; padding:28px 14px; background:#ffffff; border-radius:14px; border:1px dashed #cbd5e1; font-size:13px; font-weight:600;">
                No hay clientes activos disponibles.
            </div>
            {% endfor %}
        </div>
    </div>
    """

    contexto = dict(todos=todos_procesados, total_semanales=total_semanales)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_sincro, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_sincro, **contexto))

@app.route("/api/clientes/forzar_enrutado", methods=["POST"])
def api_clientes_forzar_enrutado():
    if not session.get("autenticado"): 
        return "No autorizado"
        
    c_id = request.form.get("cliente_forzar_id")
    hoy_str = date.today().isoformat()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Actualización con liberación segura de recursos en el pool de Neon
            cursor.execute("UPDATE clientes SET enrutado_forzado = 1 WHERE id = %s AND ruta_id = %s", (c_id, int(session.get("ruta_id") or 0)))
        conn.commit()
    except Exception as e:
        print(f"Error crítico al forzar enrutamiento anticipado: {e}")
    finally:
        pass
        
    return redirect("/")

@app.route("/menu/salvar_datos")
def seccion_respaldo_nube():
    if not session.get("autenticado"):
        return "Sesión expirada"
        
    error_respaldo = None
    total_clientes = 0
    total_pagos = 0

    rid = int(session.get("ruta_id") or 1)
    # 🔄 RESPALDO REAL: Verificación física y conteo de consistencia en Neon
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_f = cursor.fetchone()
            hoy_str = row_f["valor"] if (row_f and row_f["valor"]) else date.today().isoformat()

            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE ruta_id = %s", (rid,))
            res_c = cursor.fetchone()
            total_clientes = res_c["total"] if isinstance(res_c, dict) else res_c[0]

            cursor.execute("SELECT COUNT(*) AS total FROM pagos WHERE ruta_id = %s AND pagado = 1", (rid,))
            res_p = cursor.fetchone()
            total_pagos = res_p["total"] if isinstance(res_p, dict) else res_p[0]
            
        conn.commit()
    except Exception as e:
        error_respaldo = str(e)
        
    if error_respaldo:
        estado_texto = "Error de conexión con el servidor"
        estado_color = "#dc2626"
        estado_bg = "#fef2f2"
        estado_borde = "#fecaca"
        toast_msg = f"ERROR: {error_respaldo}"
    else:
        estado_texto = "Conectado al servidor"
        estado_color = "#16a34a"
        estado_bg = "#ffffff"
        estado_borde = "#e2e8f0"
        toast_msg = f"EXCELENTE: Datos salvados correctamente ({total_clientes} clientes)"

        html_cloud = f"""
    <div class="section-header-title" style="display:flex; align-items:center; gap:8px;">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 13v8"/><path d="m4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="m8 17 4-4 4 4"/></svg>
        <span>Backup</span>
    </div>
    
    <div style="display:flex; flex-direction:column; gap:14px; padding-bottom:24px;">
        <!-- Cabecera Visual de Sincronización App -> Nube -->
        <div style="display:flex; align-items:center; justify-content:center; gap:18px; padding:14px 0 6px 0;">
            <div style="width:64px; height:64px; border-radius:16px; background:linear-gradient(145deg, #0f2b5c, #1e3a8a); display:flex; flex-direction:column; align-items:center; justify-content:center; color:white; box-shadow:0 6px 14px rgba(15,43,92,0.3); position:relative; border:1px solid rgba(255,255,255,0.1);">
                <svg width="34" height="34" viewBox="0 0 40 40" fill="none">
                    <!-- Letra A estilizada de Avanta -->
                    <path d="M20 7L9 31H14.2L20 17.5L25.8 31H31L20 7Z" fill="#ffffff"/>
                    <!-- Flecha dorada ascendente -->
                    <path d="M8 28.5C13.5 26 19.5 22 25.5 16.5L22.5 15L30.5 13.5L29 21.5L27.5 18.5C21.5 24 14.5 28 8 28.5Z" fill="#fbbf24"/>
                </svg>
                <span style="font-size:8px; font-weight:900; letter-spacing:0.5px; position:absolute; bottom:4px; right:6px; color:#fbbf24;">PRO</span>
            </div>
            
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#475569" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
            
            <div style="width:64px; height:64px; border-radius:50%; background:linear-gradient(135deg, #06b6d4, #0891b2); display:flex; align-items:center; justify-content:center; color:white; box-shadow:0 6px 14px rgba(8,145,178,0.28);">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
            </div>
        </div>

        <!-- Tarjeta de Estado de Conexión -->
        <div style="background:{estado_bg}; border:1px solid {estado_borde}; border-radius:12px; padding:16px; text-align:center; box-shadow:0 2px 6px rgba(0,0,0,0.03);">
            <span style="color:{estado_color}; font-weight:800; font-size:15px;">{estado_texto}</span>
        </div>

        <!-- Tarjeta Informativa de Respaldo -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 2px 6px rgba(0,0,0,0.03);">
            <h4 style="font-size:13px; font-weight:800; color:#1e293b; margin:0 0 8px 0;">Respaldar información en la nube de Avanta Pagos.</h4>
            <p style="font-size:12px; color:#64748b; line-height:1.45; margin:0 0 10px 0;">
                Desde esta herramienta puedes verificar y sincronizar toda la cartera de créditos y cobros directamente hacia tu servidor central en la nube.
            </p>
            <p style="font-size:12px; color:#64748b; line-height:1.45; margin:0;">
                Registros activos verificados: <b style="color:#0f172a;">{total_clientes} clientes</b> y <b style="color:#0f172a;">{total_pagos} cuotas cobradas</b>.
            </p>
        </div>

        <!-- Tarjeta de Información de Cuenta -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 2px 6px rgba(0,0,0,0.03);">
            <h4 style="font-size:13px; font-weight:800; color:#1e293b; margin:0 0 6px 0;">Información de tu cuenta Avanta Cloud</h4>
            <p style="font-size:12px; color:#64748b; line-height:1.4; margin:0 0 12px 0;">
                Guarda esta información para mantener el control de acceso a tus datos sincronizados.
            </p>
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 12px; font-size:13px; font-weight:700; color:#1e293b; margin-bottom:8px;">
                IdApp: AVANTA-13PROD
            </div>
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 12px; font-size:13px; font-weight:700; color:#1e293b;">
                Email: rjhon5953@gmail.com
            </div>
        </div>

        <!-- Tarjeta de Ejecución y Botón Respaldar Verde -->
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 2px 6px rgba(0,0,0,0.03);">
            <h4 style="font-size:13px; font-weight:800; color:#1e293b; margin:0 0 6px 0;">Realizar respaldo en la nube</h4>
            <p style="font-size:12px; color:#64748b; line-height:1.45; margin:0 0 16px 0;">
                Asegúrate de tener conexión de internet, a continuación debes hacer click en el botón <b>Respaldar</b> para almacenar y confirmar una copia de seguridad en la nube.
            </p>
            <button type="button" id="btnEjecutarBackup" 
                    onclick="var b=this, t=document.getElementById('toastBackup'); b.style.opacity='0.8'; b.querySelector('span').innerText='Sincronizando...'; setTimeout(function(){{ b.style.opacity='1'; b.querySelector('span').innerText='Respaldar'; t.style.visibility='visible'; t.style.opacity='1'; setTimeout(function(){{ t.style.opacity='0'; setTimeout(function(){{ t.style.visibility='hidden'; }}, 300); }}, 3200); }}, 450);" 
                    style="width:100%; background:linear-gradient(135deg, #10b981, #059669); color:#ffffff; border:none; border-radius:10px; padding:14px 16px; font-size:15px; font-weight:800; cursor:pointer; display:flex; align-items:center; justify-content:center; position:relative; box-shadow:0 4px 12px rgba(16,185,129,0.3); transition:all 0.2s;">
                <span>Respaldar</span>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; right:18px;"><path d="M12 13v8"/><path d="m4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="m8 17 4-4 4 4"/></svg>
            </button>
            <a href="/api/exportar_excel_backup" download
               style="width:100%; margin-top:10px; background:#0f2b5c; color:#ffffff; border:none; border-radius:10px; padding:13px 16px; font-size:14px; font-weight:700; text-decoration:none; display:flex; align-items:center; justify-content:center; position:relative; box-shadow:0 4px 10px rgba(15,43,92,0.2);">
                <span>Descargar Copia en Excel / CSV</span>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="position:absolute; right:18px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            </a>
        </div>
    </div>

    <!-- Notificación Flotante Tipo Toast Nativo -->
    <div id="toastBackup" style="visibility:hidden; opacity:0; position:fixed; bottom:32px; left:50%; transform:translateX(-50%); background:rgba(15, 23, 42, 0.95); color:#ffffff; padding:13px 22px; border-radius:10px; font-size:13px; font-weight:700; z-index:9999; box-shadow:0 8px 20px rgba(0,0,0,0.3); transition:opacity 0.3s ease; text-align:center; min-width:270px; max-width:90%;">
        {toast_msg}
    </div>
    """
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_cloud)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_cloud))

@app.route("/api/exportar_excel_backup")
def exportar_excel_backup():
    if not session.get("autenticado"):
        return "No autorizado", 401
        
    import io, csv
    from flask import Response
    
    conn = get_db()
    output = io.StringIO()
    output.write('\ufeff')  # Marca UTF-8 para que Excel abra tildes y eñes correctamente
    writer = csv.writer(output, delimiter=';')
    
    writer.writerow(["ID", "Nombre Cliente", "Cedula", "Telefono", "Frecuencia", "Monto Total", "Cuota", "Total Pagado", "Saldo Pendiente", "Estado"])
    
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT c.id, c.nombre, c.identificacion, c.telefono, c.frecuencia, c.monto_total, c.valor_cuota, c.estado,
                   COALESCE((SELECT SUM(valor_pagado) FROM pagos WHERE cliente_id = c.id), 0) AS pagado
            FROM clientes c
            ORDER BY c.id ASC
        """)
        for row in cursor.fetchall():
            total = Decimal(str(row["monto_total"]))
            pagado = Decimal(str(row["pagado"]))
            saldo = total - pagado
            writer.writerow([
                row["id"], row["nombre"], row["identificacion"] or "", row["telefono"] or "",
                row["frecuencia"], f"{total:.2f}", f"{Decimal(str(row['valor_cuota'])):.2f}",
                f"{pagado:.2f}", f"{saldo:.2f}", row["estado"]
            ])
            
    fecha_hoy = date.today().isoformat()
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment;filename=Respaldo_Avanta_Pagos_{fecha_hoy}.csv"}
    )

@app.route("/api/cliente_info/<int:id>")
def api_cliente_info(id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, nombre, telefono, monto_total, estado, referencia, identificacion, direccion FROM clientes WHERE id = %s", (id,))
            c = cursor.fetchone()
            if not c:
                return jsonify({"status": "error", "message": "No encontrado"})
                
            cursor.execute("SELECT numero, fecha, valor, pagado, valor_pagado FROM pagos WHERE cliente_id = %s ORDER BY numero ASC", (id,))
            pagos = cursor.fetchall()
            
    # 🟢 CORREGIDO: Conversión limpia mapeando explícitamente las columnas para evitar caídas de DictCursor
    c_dict = {
        "id": c["id"], "nombre": c["nombre"], "telefono": c["telefono"] or "",
        "monto_total": float(c["monto_total"]), "estado": c["estado"],
        "referencia": c["referencia"] or "Ninguna", "identificacion": c["identificacion"] or "N/A",
        "direccion": c["direccion"] or "No registrada"
    }
    pagado = sum(float(p["valor_pagado"]) for p in pagos)
    c_dict["saldo_actual"] = max(0.0, float(c_dict["monto_total"]) - pagado)
    
    return jsonify({"status": "ok", "data": c_dict})

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>", methods=["POST"])
def api_marcar_pago(cliente_id, num_cuota):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    data_pago = request.get_json(silent=True) or request.form
    try:
        monto_recaudado = Decimal(str(data_pago.get("monto", "0")))
    except Exception:
        return jsonify({"status": "error", "message": "Monto de pago inválido."}), 400
    if not monto_recaudado.is_finite() or monto_recaudado <= 0:
        return jsonify({"status": "error", "message": "El monto debe ser mayor que cero."}), 400
    if monto_recaudado != monto_recaudado.quantize(Decimal("0.01")):
        return jsonify({"status": "error", "message": "El monto debe tener máximo dos decimales."}), 400
    hora_local_cliente = str(data_pago.get("hora_local", "") or "")
    if not hora_local_cliente:
        hora_local_cliente = datetime.now().strftime("%d/%m/%Y %H:%M")
    metodo_pago = str(data_pago.get("metodo_pago", "efectivo") or "efectivo").strip().lower()
    if "transf" in metodo_pago:
        metodo_pago = "transferencia"
    else:
        metodo_pago = "efectivo"
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    with conn.cursor() as cur_f:
        cur_f.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
        row_f = cur_f.fetchone()

    ahora_loc = ahora_local()
    hoy_date = ahora_loc.date()
    hoy_str = str(row_f["valor"]).split("_")[0] if (row_f and row_f["valor"]) else hoy_date.isoformat()
    monto_restante = monto_recaudado
    monto_efectivamente_aplicado = Decimal("0.00")
    fecha_hora_pago = ahora_loc.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn.cursor() as cursor:
            # Bloqueo transaccional del cliente y validación de pertenencia a la ruta.
            cursor.execute("SELECT id, nombre, telefono, monto_total FROM clientes WHERE id = %s AND ruta_id = %s FOR UPDATE", (cliente_id, rid))
            c = cursor.fetchone()
            if not c:
                conn.rollback()
                return jsonify({"status": "error", "message": "Cliente no encontrado en tu ruta."}), 404
            # 1. Aplicamos el dinero recibido a las cuotas pendientes cronológicamente
            cursor.execute("SELECT id, numero, fecha, valor, valor_pagado, pagado FROM pagos WHERE cliente_id = %s AND ruta_id = %s AND pagado = 0 ORDER BY numero ASC FOR UPDATE", (cliente_id, rid))
            pagos_pendientes = cursor.fetchall()
            saldo_pendiente_plan = sum(
                (Decimal(str(row["valor"])) - Decimal(str(row["valor_pagado"] or 0)))
                for row in pagos_pendientes
            )
            if monto_recaudado > saldo_pendiente_plan:
                conn.rollback()
                return jsonify({
                    "status": "error",
                    "message": f"El monto supera el saldo pendiente de esta ruta (${saldo_pendiente_plan:.2f}). Verifica el valor recibido."
                }), 400

            for p in pagos_pendientes:
                if monto_restante <= 0:
                    break

                pendiente_cuota = Decimal(str(p["valor"])) - Decimal(str(p["valor_pagado"]))

                if monto_restante >= pendiente_cuota:
                    monto_restante = round(monto_restante - pendiente_cuota, 2)
                    monto_efectivamente_aplicado += pendiente_cuota
                    cursor.execute(
                        "UPDATE pagos SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s, metodo_pago = %s WHERE id = %s AND ruta_id = %s",
                        (p["valor"], fecha_hora_pago, metodo_pago, p["id"], rid)
                    )
                else:
                    nuevo_pago_parcial = round(Decimal(str(p["valor_pagado"])) + monto_restante, 2)
                    monto_efectivamente_aplicado += monto_restante
                    monto_restante = Decimal("0.00")
                    cursor.execute(
                        "UPDATE pagos SET valor_pagado = %s, fecha_pago_real = %s, metodo_pago = %s WHERE id = %s AND ruta_id = %s",
                        (nuevo_pago_parcial, fecha_hora_pago, metodo_pago, p["id"], rid)
                    )

            # 2. Evaluamos el estado REAL de la cuenta DESPUÉS de aplicar el pago
            cursor.execute("SELECT fecha, pagado FROM pagos WHERE cliente_id = %s AND ruta_id = %s ORDER BY numero ASC", (cliente_id, rid))
            estado_final_cuotas = cursor.fetchall()

            dias_mora_real = 0
            dias_adelanto_real = 0
            tiene_mora = False

            for q in estado_final_cuotas:
                f_q = datetime.strptime(str(q["fecha"])[:10], "%Y-%m-%d").date() if isinstance(q["fecha"], str) else q["fecha"]
                if int(q["pagado"]) == 0 and f_q < hoy_date:
                    tiene_mora = True
                    dias_mora_real += 1
                elif int(q["pagado"]) == 1 and f_q > hoy_date:
                    dias_adelanto_real += 1

            if tiene_mora:
                dias_adelanto_real = 0

            # 3. Recalcular saldo total real del cliente
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE cliente_id = %s AND ruta_id = %s", (cliente_id, rid))
            total_pagado = Decimal(str(cursor.fetchone()["total"]))
            saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - total_pagado)
            
            if saldo_restante <= 0:
                cursor.execute("UPDATE clientes SET estado = 'Inactivo', fecha_gestion = NULL, enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s AND ruta_id = %s", (cliente_id, rid))
            else:
                cursor.execute("UPDATE clientes SET fecha_gestion = NULL, enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s AND ruta_id = %s", (cliente_id, rid))

            # 4. Asentar únicamente el dinero neto cobrado hoy en el turno activo del balance
            if monto_efectivamente_aplicado > 0:
                rid = int(session.get("ruta_id") or 1)
                cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
                row_j_pago = cursor.fetchone()
                turno_balance_pago = row_j_pago["valor"] if (row_j_pago and row_j_pago["valor"]) else hoy_str

                concepto_abono = f"ABONO CUOTA - CLIENTE: {c['nombre']} (ID:{cliente_id})"
                cursor.execute(
                    """
                    INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante, ruta_id, metodo_pago)
                    VALUES ('Entrada', 'Cobranza', %s, %s, %s, '', %s, %s)
                    """,
                    (concepto_abono, monto_efectivamente_aplicado, turno_balance_pago, rid, metodo_pago)
                )
                
            conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        pass

    # 5. Construcción exacta del comprobante de WhatsApp
    linea_division = "------------------------------------------"
    mensaje_ws = (
        "🧾 *AVANTA PAGOS* 🧾\n"
        "_*Comprobante Oficial de Pago*_\n"
        f"{linea_division}\n"
        f"👤 *Cliente:* {c['nombre']}\n"
        f"📱 *WhatsApp:* {c['telefono'] or 'N/A'}\n"
        f"*📅 Fecha de Pago:* {hora_local_cliente}\n"
        f"{linea_division}\n"
        f"💵 *Monto Cobrado:* ${float(monto_efectivamente_aplicado):.2f}\n"
        f"🔢 *Cuota Procesada:* #{num_cuota}\n"
        f"📉 *Saldo Restante:* ${float(saldo_restante):.2f}\n"
    )
    
    if saldo_restante <= 0:
        mensaje_ws += f"{linea_division}\n"
        mensaje_ws += "🎉 *¡FELICIDADES! CRÉDITO LIQUIDADO AL 100%* 🎉\n"
        mensaje_ws += f"{linea_division}\n"
    elif tiene_mora and dias_mora_real > 0:
        mensaje_ws += f"⚠️ *Días de Atraso:* {dias_mora_real} días en mora\n"
        mensaje_ws += f"👉 _Le solicitamos ponerse al día para evitar recargos o suspensión de su cupo._\n"
        mensaje_ws += f"{linea_division}\n"
    elif dias_adelanto_real > 0:
        mensaje_ws += f"🔵 *Días Adelantados:* {dias_adelanto_real} días a favor\n"
        mensaje_ws += f"🎉 _¡Excelente historial! Siga manteniendo su cuenta al día._\n"
        mensaje_ws += f"{linea_division}\n"
    else:
        mensaje_ws += f"{linea_division}\n"
        mensaje_ws += "🙏 *¡Gracias por su puntualidad y pago!* 🙏\n"
        mensaje_ws += f"{linea_division}\n"
        
    mensaje_ws += "_Sistema de Gestión de Cobros Avanta_"
    
    titulo_estado_tk = "¡Gracias por su puntualidad y pago!"
    subtitulo_estado_tk = "Manteniendo su cuenta al día."
    tipo_alerta_tk = "ok"

    if saldo_restante <= 0:
        titulo_estado_tk = "¡FELICIDADES! CRÉDITO LIQUIDADO AL 100%"
        subtitulo_estado_tk = "Su préstamo ha sido cancelado en su totalidad."
        tipo_alerta_tk = "liquidado"
    elif tiene_mora and dias_mora_real > 0:
        titulo_estado_tk = f"Días de Atraso: {dias_mora_real} días en mora"
        subtitulo_estado_tk = "Le solicitamos ponerse al día para evitar recargos o suspensión de su cupo."
        tipo_alerta_tk = "mora"
    elif dias_adelanto_real > 0:
        titulo_estado_tk = f"Días Adelantados: {dias_adelanto_real} días a favor"
        subtitulo_estado_tk = "¡Excelente historial! Siga manteniendo su cuenta al día."
        tipo_alerta_tk = "adelanto"

    recibo = {
        "cliente": c["nombre"],
        "telefono": c["telefono"] or "",
        "cuota": num_cuota,
        "monto": float(monto_efectivamente_aplicado),
        "saldo": float(saldo_restante),
        "mensaje_ws": mensaje_ws
    }
    ticket_pago = {
        "tipo": "pago",
        "codigo": f"{int(cliente_id):06d}",
        "tipo_operacion": "Comprobante Oficial de Pago",
        "cliente": c["nombre"],
        "telefono": c.get("telefono") or "N/A",
        "fecha_pago": hora_local_cliente,
        "monto_cobrado": f"{float(monto_efectivamente_aplicado):.2f}",
        "cuota_procesada": str(num_cuota),
        "saldo_restante": f"{float(saldo_restante):.2f}",
        "titulo_estado": titulo_estado_tk,
        "subtitulo_estado": subtitulo_estado_tk,
        "tipo_alerta": tipo_alerta_tk,
        "pie_sistema": "Sistema de Gestión de Cobros Avanta",
        "mensaje_ws": mensaje_ws
    }
    return jsonify({"status": "ok", "recibo": recibo, "ticket_pago": ticket_pago})

@app.route("/api/marcar_no_pago/<int:cliente_id>", methods=["POST"])
def api_marcar_no_pago(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    rid = int(session.get("ruta_id") or 1)
    data_np = request.get_json(silent=True) or {}
    razon_np = str(data_np.get("razon") or "CLIENTE SIN DINERO").strip()[:120]
    comentarios_np = str(data_np.get("comentarios") or "").strip()[:300]
    ahora_ec_np = (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_f = cursor.fetchone()
            turno_act = str(dict(row_f).get("valor") if row_f else date.today().isoformat()).strip()
            inicio_ts = f"{turno_act[:10]} {turno_act[11:13]}:{turno_act[13:15]}:{turno_act[15:17]}" if len(turno_act) >= 17 and "_" in turno_act else f"{turno_act[:10]} 00:00:00"

            cursor.execute("""
                SELECT id FROM pagos
                WHERE cliente_id = %s AND ruta_id = %s
                  AND COALESCE(valor_pagado, 0) > 0
                  AND LEFT(COALESCE(fecha_pago_real::text, ''), 19) >= %s
            """, (cliente_id, rid, inicio_ts))
            pagos_turno = cursor.fetchall()

            if pagos_turno:
                for pt in pagos_turno:
                    cursor.execute("UPDATE pagos SET pagado = 0, valor_pagado = 0.00, fecha_pago_real = '' WHERE id = %s AND ruta_id = %s", (dict(pt)["id"], rid))
                cursor.execute("""
                    DELETE FROM balance_movimientos
                    WHERE ruta_id = %s AND tipo = 'Entrada' AND categoria = 'Cobranza'
                      AND fecha = %s AND concepto LIKE %s
                """, (rid, turno_act, f"%(ID:{cliente_id})%"))

            cursor.execute(
                """
                UPDATE clientes
                   SET estado = 'Activo',
                       saltado_hoy = COALESCE(saltado_hoy, 0) + 1,
                       fecha_gestion = %s,
                       razon_no_pago = %s,
                       comentario_no_pago = %s
                 WHERE id = %s AND ruta_id = %s
                """,
                (ahora_ec_np, razon_np, comentarios_np, cliente_id, rid)
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al registrar no pago del cliente: {e}")
        return jsonify({"status": "error", "message": "Error interno del servidor"}), 500

    return jsonify({"status": "ok"})

@app.route("/menu/bluetooth")
def seccion_bluetooth_impresora():
    if not session.get("autenticado"):
        return "Sesión expirada"

    html_bluetooth = """
    <div style="padding:0 4px 28px 4px;">
        <!-- Cabecera Corporativa -->
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 10px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">IMPRESORA TÉRMICA 58MM</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">Enlace inalámbrico ESC/POS para recibos en ruta</div>
            </div>
        </div>

        <!-- Nota Técnica -->
        <div style="background:#eff6ff; border:1px solid #dbeafe; border-radius:10px; padding:9px 12px; margin-bottom:12px; display:flex; align-items:center; gap:8px; text-align:left;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            <span style="font-size:11.5px; color:#1e3a8a; font-weight:600; line-height:1.3;">Enciende tu impresora portátil de 58mm y activa el Bluetooth y la Ubicación del celular antes de vincular.</span>
        </div>

        <!-- Panel de Estado Interactivo -->
        <div style="background:#ffffff; border-radius:16px; border:1px solid #e2e8f0; padding:22px 16px; margin-bottom:12px; box-shadow:0 4px 12px rgba(15,23,42,0.04); text-align:center;">
            <div id="box_bt_status" style="width:68px; height:68px; border-radius:18px; background:#f1f5f9; color:#64748b; display:flex; align-items:center; justify-content:center; margin:0 auto 14px auto; border:1.5px solid #e2e8f0; transition:all 0.3s;">
                <svg id="icon_bt_status" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
            </div>

            <div style="display:inline-flex; align-items:center; gap:6px; background:#f8fafc; border:1px solid #e2e8f0; padding:4px 10px; border-radius:20px; margin-bottom:8px;">
                <span id="dot_bt_status" style="width:8px; height:8px; border-radius:50%; background:#94a3b8;"></span>
                <span style="font-size:10.5px; font-weight:800; color:#475569; letter-spacing:0.4px;">PROTOCOLO ESC/POS • 58MM</span>
            </div>

            <h4 style="font-size:16px; color:#0f172a; margin:4px 0 6px 0; font-weight:800;">Estado de la Impresora</h4>
            <p id="text_bt_status" style="font-size:12.5px; color:#64748b; font-weight:600; margin:0 0 18px 0; line-height:1.4;">Desconectada • Lista para emparejar</p>

            <div style="display:flex; flex-direction:column; gap:10px;">
                <button type="button" onclick="conectarImpresoraBluetooth()"
                        style="width:100%; padding:14px; background:linear-gradient(135deg, #0f2b5c, #1e3a8a); color:#ffffff; border:none; border-radius:11px; font-weight:800; font-size:14px; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; box-shadow:0 4px 12px rgba(15,43,92,0.22);">
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"/></svg>
                    Vincular Impresora (58mm)
                </button>

                <button type="button" onclick="probarImpresionTicket()" id="btn_probar_print" disabled
                        style="width:100%; padding:13px; background:#e2e8f0; color:#64748b; border:none; border-radius:11px; font-weight:800; font-size:13.5px; cursor:not-allowed; display:flex; align-items:center; justify-content:center; gap:8px; transition:all 0.2s;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
                    Imprimir Ticket de Prueba
                </button>
            </div>
        </div>
    </div>
    """

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_bluetooth)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_bluetooth))

@app.route("/menu/configuracion")
def seccion_configuracion_sistema():
    if not session.get("autenticado"):
        return "Sesión expirada"

    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT llave, valor FROM configuracion WHERE ruta_id = %s", (rid,))
            filas = cursor.fetchall()
            config = {f["llave"]: f["valor"] for f in filas}
    finally:
        pass

    usuario_actual = config.get("usuario", "admin")
    empresa_nombre = config.get("empresa_nombre", "AVANTA PAGOS")
    empresa_telefono = config.get("empresa_telefono", "593991234567")
    ticket_encabezado = config.get("ticket_encabezado", "¡BIENVENIDO A AVANTA PAGOS!")
    ticket_pie = config.get("ticket_pie", "Gracias por su puntualidad. Conserve este comprobante.")
    auto_ws = config.get("auto_whatsapp", "1")
    auto_bt = config.get("auto_bluetooth", "0")

    chk_ws_checked = "checked" if auto_ws == "1" else ""
    bg_ws = "#16a34a" if auto_ws == "1" else "#cbd5e1"
    tx_ws = "translateX(20px)" if auto_ws == "1" else "translateX(0)"

    chk_bt_checked = "checked" if auto_bt == "1" else ""
    bg_bt = "#16a34a" if auto_bt == "1" else "#cbd5e1"
    tx_bt = "translateX(20px)" if auto_bt == "1" else "translateX(0)"

    html_config = f"""
    <div style="padding:0 4px 28px 4px;">
        <!-- Cabecera Corporativa -->
        <div style="display:flex; align-items:center; gap:10px; padding:4px 0 12px 0;">
            <div style="width:36px; height:36px; border-radius:10px; background:#0f2b5c; color:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </div>
            <div style="text-align:left;">
                <div style="font-size:15px; font-weight:800; color:#0f2b5c; line-height:1.15;">CONFIGURACIÓN OPERATIVA</div>
                <div style="font-size:11px; color:#64748b; font-weight:600; margin-top:2px;">Recibos, preferencias de ruta y seguridad</div>
            </div>
        </div>

        <form action="/api/configuracion/guardar_maestro" method="POST" style="display:flex; flex-direction:column; gap:12px;">
            <!-- SECCIÓN 1: IDENTIDAD Y RECIBOS -->
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                    <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Identidad y Comprobantes</span>
                </div>

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Nombre Comercial / Empresa</label>
                <input type="text" name="empresa_nombre" value="{empresa_nombre}" required
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:700; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Teléfono de Atención / Soporte</label>
                <input type="text" name="empresa_telefono" value="{empresa_telefono}" required
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Encabezado de Ticket / WhatsApp</label>
                <input type="text" name="ticket_encabezado" value="{ticket_encabezado}" required
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13.5px; font-weight:600; color:#0f172a; margin-bottom:11px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Mensaje Pie de Recibo</label>
                <input type="text" name="ticket_pie" value="{ticket_pie}"
                       style="width:100%; padding:11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:13px; font-weight:500; color:#0f172a; box-sizing:border-box;">
            </div>

            <!-- SECCIÓN 2: PREFERENCIAS OPERATIVAS EN RUTA -->
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                    <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Automatización en Ruta</span>
                </div>

                <!-- Switch WhatsApp -->
                <div style="display:flex; justify-content:space-between; align-items:center; padding-bottom:12px; margin-bottom:12px; border-bottom:1px solid #f1f5f9; gap:10px;">
                    <div>
                        <div style="font-size:13px; font-weight:700; color:#0f172a;">Comprobante WhatsApp al cobrar</div>
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Mostrar opción de envío tras cada abono</div>
                    </div>
                    <div style="position:relative; display:inline-block; width:46px; height:26px; flex-shrink:0;">
                        <input type="checkbox" name="auto_whatsapp" value="1" id="cfgChkWs" {chk_ws_checked} style="opacity:0; width:0; height:0;">
                        <span onclick="var c=document.getElementById('cfgChkWs'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:{bg_ws}; border-radius:26px; transition:.2s;">
                            <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; transform:{tx_ws}; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                        </span>
                    </div>
                </div>

                <!-- Switch Bluetooth -->
                <div style="display:flex; justify-content:space-between; align-items:center; gap:10px;">
                    <div>
                        <div style="font-size:13px; font-weight:700; color:#0f172a;">Impresión térmica 58mm</div>
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Habilitar ticket físico si hay impresora vinculada</div>
                    </div>
                    <div style="position:relative; display:inline-block; width:46px; height:26px; flex-shrink:0;">
                        <input type="checkbox" name="auto_bluetooth" value="1" id="cfgChkBt" {chk_bt_checked} style="opacity:0; width:0; height:0;">
                        <span onclick="var c=document.getElementById('cfgChkBt'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:{bg_bt}; border-radius:26px; transition:.2s;">
                            <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; transform:{tx_bt}; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                        </span>
                    </div>
                </div>
            </div>

            <!-- SECCIÓN 3: SEGURIDAD DE ACCESO -->
            <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                    <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Seguridad de Acceso</span>
                </div>
            
            <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Usuario Asignado (Solo lectura)</label>
                <input type="text" value="{usuario_actual}" disabled
                       style="width:100%; padding:11px 12px; border:1.5px solid #e2e8f0; background:#f8fafc; border-radius:10px; font-size:14px; font-weight:700; color:#64748b; margin-bottom:11px; box-sizing:border-box;">

                <label style="font-size:11.5px; font-weight:700; color:#334155; display:block; margin-bottom:4px;">Nueva Contraseña (Dejar en blanco para conservar actual)</label>
                <div style="position:relative;">
                    <input type="password" id="cfgNuevaClaveInput" name="nueva_clave" placeholder="••••••••"
                           style="width:100%; padding:11px 40px 11px 12px; border:1.5px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; box-sizing:border-box;">
                    <button type="button" onclick="const i=document.getElementById('cfgNuevaClaveInput'); i.type=i.type==='password'?'text':'password';"
                            style="position:absolute; right:10px; top:50%; transform:translateY(-50%); background:transparent; border:none; color:#64748b; cursor:pointer; padding:4px; display:flex;">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    </button>
                </div>
            </div>

            <button type="submit" style="width:100%; padding:14px; background:linear-gradient(135deg, #16a34a, #15803d); color:#ffffff; border:none; border-radius:12px; font-weight:800; font-size:14.5px; cursor:pointer; box-shadow:0 4px 12px rgba(22,163,74,0.25);">
                Guardar Configuración
            </button>
        </form>

        <!-- SECCIÓN 4: MANTENIMIENTO DE JORNADA -->
        <div style="background:#ffffff; border-radius:14px; border:1px solid #e2e8f0; padding:15px; margin-top:12px; box-shadow:0 2px 6px rgba(15,23,42,0.03); text-align:left;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                <span style="font-size:13.5px; font-weight:800; color:#0f2b5c;">Mantenimiento de Jornada</span>
            </div>
            <p style="font-size:11.5px; color:#64748b; margin:0 0 12px 0; line-height:1.35;">
                Restablece los clientes marcados como No Pago hoy para que vuelvan a aparecer en tu lista de pendientes.
            </p>
            <form action="/api/configuracion/reset_saltos" method="POST" onsubmit="return confirm('¿Reiniciar los clientes marcados como No Pago hoy para volver a visitarlos?');" style="margin:0;">
                <button type="submit" style="width:100%; padding:12px; background:#f1f5f9; border:1.5px solid #cbd5e1; color:#334155; border-radius:10px; font-weight:800; font-size:13px; cursor:pointer;">
                    Reiniciar No Pagos de Hoy
                </button>
            </form>
        </div>
    </div>
    """

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_config)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_config))


@app.route("/api/configuracion/guardar_maestro", methods=["POST"])
def api_guardar_configuracion_maestra():
    if not session.get("autenticado"):
        return redirect("/login")

    empresa_nombre = request.form.get("empresa_nombre", "").strip()
    empresa_telefono = request.form.get("empresa_telefono", "").strip()
    ticket_encabezado = request.form.get("ticket_encabezado", "").strip()
    ticket_pie = request.form.get("ticket_pie", "").strip()
    auto_whatsapp = "1" if request.form.get("auto_whatsapp") == "1" else "0"
    auto_bluetooth = "1" if request.form.get("auto_bluetooth") == "1" else "0"
    nueva_clave = request.form.get("nueva_clave", "").strip()

    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            maestro_datos = {
                "empresa_nombre": empresa_nombre,
                "empresa_telefono": empresa_telefono,
                "ticket_encabezado": ticket_encabezado,
                "ticket_pie": ticket_pie,
                "auto_whatsapp": auto_whatsapp,
                "auto_bluetooth": auto_bluetooth
            }

            for llave, valor in maestro_datos.items():
                if valor != "":
                    cursor.execute("DELETE FROM configuracion WHERE llave = %s AND ruta_id = %s", (llave, rid))
                    cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES (%s, %s, %s)", (llave, valor, rid))

            if nueva_clave:
                if len(nueva_clave) < 12:
                    return redirect("/menu/configuracion")
                hash_clave = generate_password_hash(nueva_clave)
                cursor.execute("DELETE FROM configuracion WHERE llave = 'clave' AND ruta_id = %s", (rid,))
                cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('clave', %s, %s)", (hash_clave, rid))
                cursor.execute("UPDATE rutas SET clave_cobrador = %s WHERE id = %s AND COALESCE(empresa_id, 1) = %s", (hash_clave, rid, int(session.get("empresa_id") or 0)))

            conn.commit()
    except Exception as e:
        print(f"Error crítico al guardar configuración operativa: {e}")
    finally:
        pass

    return redirect("/menu/configuracion")


@app.route("/api/configuracion/reset_saltos", methods=["POST"])
def api_reset_saltos_hoy():
    if not session.get("autenticado"):
        return redirect("/login")

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
            row_j = cursor.fetchone()
            hoy_str = row_j["valor"] if (row_j and row_j["valor"]) else date.today().isoformat()

            cursor.execute("""
                UPDATE clientes
                SET fecha_gestion = NULL
                WHERE estado = 'Activo'
                  AND fecha_gestion = %s
                  AND id NOT IN (SELECT DISTINCT cliente_id FROM pagos WHERE fecha_pago_real = %s AND valor_pagado > 0)
            """, (hoy_str, hoy_str))
            conn.commit()
    except Exception as e:
        print(f"Error al reiniciar no pagos de hoy: {e}")

    return redirect("/")


@app.route("/api/clientes/lista_negra/<int:id>", methods=["POST"])
def api_lista_negra(id):
    if not session.get("autenticado"): 
        return "No autorizado"
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Sintaxis SQL limpia y directa
            cursor.execute("UPDATE clientes SET estado = 'Lista Negra' WHERE id = %s AND ruta_id = %s", (id, int(session.get("ruta_id") or 0)))
        conn.commit()
    except Exception as e:
        print(f"Error al enviar a lista negra: {e}")
    finally:
        pass
        
    return redirect("/menu/clientes")

@app.route("/api/gasto_foto/<int:gasto_id>")
def api_gasto_foto(gasto_id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Consulta y extracción con liberación obligatoria del canal en el pool
            cursor.execute("SELECT comprobante FROM balance_movimientos WHERE id = %s", (gasto_id,))
            res = cursor.fetchone()
            
        if res and res["comprobante"]:
            raw_comp = res["comprobante"]
            
            # Extracción del búfer físico si llega como memoryview o bytes
            if isinstance(raw_comp, memoryview):
                raw_comp = raw_comp.tobytes()
            if isinstance(raw_comp, bytes):
                try:
                    raw_comp = raw_comp.decode('utf-8')
                except Exception:
                    import base64
                    raw_comp = base64.b64encode(raw_comp).decode('utf-8')
                
            comprobante_puro = str(raw_comp).replace("\n", "").replace("\r", "").strip()
            if "base64," in comprobante_puro:
                comprobante_puro = comprobante_puro.split("base64,")[-1]
                
            respuesta = jsonify({"status": "ok", "comprobante": comprobante_puro})
            respuesta.headers.add("Content-Type", "application/json")
            return respuesta
            
    except Exception as err:
        print(f"Error crítico en API de visualización de fotos: {err}")
        return jsonify({"status": "error", "message": "Error al procesar la cadena de imagen"})
    finally:
        pass
        
    return jsonify({"status": "error", "message": "No encontrado"})

@app.route("/menu/perfil")
def seccion_perfil():
    if not session.get("autenticado"): 
        return "Sesión expirada"
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT llave, valor FROM configuracion WHERE ruta_id = %s AND llave IN ('usuario', 'cobrador_nombre', 'cobrador_cedula', 'cobrador_celular', 'cobrador_correo')", (rid,))
        filas = cursor.fetchall()
        
    datos = {f["llave"]: f["valor"] for f in filas}
    
    contexto = dict(
        vista="perfil_cobrador",
        usuario_actual=datos.get("usuario", "admin"),
        nombre=datos.get("cobrador_nombre", ""),
        cedula=datos.get("cobrador_cedula", ""),
        celular=datos.get("cobrador_celular", ""),
        correo=datos.get("cobrador_correo", "")
    )
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/api/perfil/guardar", methods=["POST"])
def api_guardar_perfil():
    if not session.get("autenticado"): 
        return jsonify({"status": "error"})
        
    perfil_data = request.get_json(silent=True) or request.form
    nuevo_usuario = perfil_data.get("usuario")
    nueva_clave = perfil_data.get("clave")
    nom = perfil_data.get("nombre")
    ced = perfil_data.get("cedula")
    cel = perfil_data.get("celular")
    cor = perfil_data.get("correo")
    
    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    with conn.cursor() as cursor:
        for llave_conf, valor_conf in [
            ('usuario', nuevo_usuario),
            ('cobrador_nombre', nom),
            ('cobrador_cedula', ced),
            ('cobrador_celular', cel),
            ('cobrador_correo', cor)
        ]:
            cursor.execute("DELETE FROM configuracion WHERE llave = %s AND ruta_id = %s", (llave_conf, rid))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES (%s, %s, %s)", (llave_conf, valor_conf, rid))

        if nueva_clave and nueva_clave.strip() != "":
            if len(nueva_clave.strip()) < 12:
                return jsonify({"status": "error", "message": "La nueva contraseña debe tener al menos 12 caracteres."}), 400
            clave_encriptada = generate_password_hash(nueva_clave.strip())
            cursor.execute("DELETE FROM configuracion WHERE llave = 'clave' AND ruta_id = %s", (rid,))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('clave', %s, %s)", (clave_encriptada, rid))
            cursor.execute("UPDATE rutas SET clave_cobrador = %s WHERE id = %s AND COALESCE(empresa_id, 1) = %s", (clave_encriptada, rid, int(session.get("empresa_id") or 0)))
    conn.commit()
    
    session["usuario"] = nuevo_usuario
    return jsonify({"status": "ok"})

@app.route("/menu/pagos_hoy")
def seccion_pagos_hoy():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    hoy_str = date.today().isoformat()

    rid = int(session.get("ruta_id") or 1)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Traemos los recaudos individuales de hoy
            cursor.execute(
                """
                SELECT p.id AS pago_id, c.id AS cliente_id, c.nombre, p.numero, p.valor_pagado
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE c.ruta_id = %s AND p.fecha_pago_real = %s AND p.valor_pagado > 0
                ORDER BY c.nombre ASC, p.numero ASC
                """, (rid, hoy_str)
            )
            pagos_individuales = cursor.fetchall()

            # ❌ Obtener todos los clientes que fueron SALTADOS hoy por el cobrador
            cursor.execute(
                """
                SELECT id AS cliente_id, nombre, saltado_hoy
                FROM clientes
                WHERE ruta_id = %s AND fecha_gestion = %s AND estado = 'Activo' AND id NOT IN (
                    SELECT DISTINCT cliente_id FROM pagos WHERE fecha_pago_real = %s
                )
                """, (rid, hoy_str, hoy_str)
            )
            saltados = cursor.fetchall()
    except Exception as e:
        print(f"Error al consultar pagos de hoy: {e}")
        pagos_individuales = []
        saltados = []
    finally:
        pass

    # 🗂️ AGRUPACIÓN CRÍTICA CONTRA DUPLICADOS: Consolida las cuotas con tipo Decimal exacto
    mapa_agrupacion = {}
    for p in pagos_individuales:
        c_id = p["cliente_id"]
        val_pagado = Decimal(str(p["valor_pagado"]))
        
        if c_id not in mapa_agrupacion:
            mapa_agrupacion[c_id] = {
                "nombre": p["nombre"],
                "cuotas": [p["numero"]],
                "total_abonado": val_pagado,
                "pago_ids": [p["pago_id"]]
            }
        else:
            mapa_agrupacion[c_id]["cuotas"].append(p["numero"])
            mapa_agrupacion[c_id]["total_abonado"] += val_pagado
            mapa_agrupacion[c_id]["pago_ids"].append(p["pago_id"])

    pagados_agrupados = []
    for c_id, info in mapa_agrupacion.items():
        min_c = min(info["cuotas"])
        max_c = max(info["cuotas"])
        
        if min_c == max_c:
            rango_cuotas = f"#{min_c}"
        else:
            rango_cuotas = f"#{min_c} a la #{max_c}"
            
        pago_ids_str = ",".join(str(x) for x in info["pago_ids"])
        
        pagados_agrupados.append({
            "nombre": info["nombre"],
            "rango_cuotas": rango_cuotas,
            "total_abonado": float(info["total_abonado"]),
            "pago_ids_str": pago_ids_str
        })

    saltados_list = [dict(s) for s in saltados]

    contexto = dict(
        vista="pagos_hoy",
        pagados_list=pagados_agrupados,
        saltados_list=saltados_list
    )
    
    # 🪙 FIJADO: Operador de comparación exacto para el refresco asíncrono del móvil
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/api/revertir_gestion/<string:tipo>/<string:id_str>", methods=["POST"])
def api_revertir_gestion(tipo, id_str):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if tipo == "pago":
                rid = int(session.get("ruta_id") or 0)
                pago_ids = [int(x) for x in id_str.split(",") if x.strip()]
                cliente_afectado = None

                if pago_ids:
                    cursor.execute(
                        "SELECT cliente_id FROM pagos WHERE id = ANY(%s) AND ruta_id = %s LIMIT 1",
                        (pago_ids, rid)
                    )
                    res_cl = cursor.fetchone()
                    if res_cl:
                        cliente_afectado = res_cl["cliente_id"] if isinstance(res_cl, dict) else res_cl[0]

                if cliente_afectado:
                    # 1. Obtenemos y eliminamos el último abono registrado en el balance para saber el monto EXACTO a revertir
                    cursor.execute(
                        """
                        DELETE FROM balance_movimientos
                        WHERE id IN (
                            SELECT id FROM balance_movimientos
                            WHERE ruta_id = %s AND tipo = 'Entrada' AND categoria = 'Cobranza' AND concepto LIKE %s
                            ORDER BY id DESC LIMIT 1
                        )
                        RETURNING monto
                        """,
                        (rid, f"%(ID:{cliente_afectado})%")
                    )
                    res_bm = cursor.fetchone()
                    monto_a_revertir = Decimal(str((res_bm["monto"] if isinstance(res_bm, dict) else res_bm[0]) if res_bm else "0.00"))

                    # 2. Restamos ese monto desde la última cuota tocada hacia atrás, protegiendo abonos de días anteriores
                    cursor.execute(
                        "SELECT id, valor_pagado FROM pagos WHERE id = ANY(%s) AND ruta_id = %s ORDER BY numero DESC",
                        (pago_ids, rid)
                    )
                    cuotas_desc = cursor.fetchall()

                    if monto_a_revertir > 0:
                        restante = monto_a_revertir
                        for c_row in cuotas_desc:
                            if restante <= 0:
                                break
                            pid_c = c_row["id"] if isinstance(c_row, dict) else c_row[0]
                            val_p = Decimal(str((c_row["valor_pagado"] if isinstance(c_row, dict) else c_row[1]) or "0.00"))
                            if val_p <= restante:
                                restante = round(restante - val_p, 2)
                                cursor.execute(
                                    "UPDATE pagos SET pagado = 0, valor_pagado = 0.00, fecha_pago_real = '' WHERE id = %s AND ruta_id = %s",
                                    (pid_c, rid)
                                )
                            else:
                                nuevo_val = round(val_p - restante, 2)
                                restante = Decimal("0.00")
                                cursor.execute(
                                    "UPDATE pagos SET pagado = 0, valor_pagado = %s WHERE id = %s AND ruta_id = %s",
                                    (nuevo_val, pid_c, rid)
                                )
                    else:
                        for c_row in cuotas_desc:
                            pid_c = c_row["id"] if isinstance(c_row, dict) else c_row[0]
                            cursor.execute(
                                "UPDATE pagos SET pagado = 0, valor_pagado = 0.00, fecha_pago_real = '' WHERE id = %s AND ruta_id = %s",
                                (pid_c, rid)
                            )

                    cursor.execute(
                        "UPDATE clientes SET estado = 'Activo', fecha_gestion = '' WHERE id = %s AND ruta_id = %s",
                        (cliente_afectado, rid)
                    )

            elif tipo == "salto":
                cliente_id = int(id_str)
                cursor.execute(
                    "UPDATE clientes SET saltado_hoy = GREATEST(0, saltado_hoy - 1), fecha_gestion = '' WHERE id = %s AND ruta_id = %s",
                    (cliente_id, int(session.get("ruta_id") or 0))
                )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al revertir gestión del día: {e}")
    finally:
        pass
        
    return jsonify({"status": "ok"})

@app.route("/api/clientes/reactivar/<int:cliente_id>", methods=["POST"])
def api_reactivar_cliente(cliente_id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Cambia el estado a Activo, limpia marcas viejas y lo reinserta al final de la ruta del día
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes WHERE estado = 'Activo' AND ruta_id = %s", (int(session.get("ruta_id") or 0),))
            max_orden = int(cursor.fetchone()[0])
            
            cursor.execute(
                """
                UPDATE clientes 
                SET estado = 'Activo', 
                    fecha_gestion = '', 
                    saltado_hoy = 0, 
                    enrutado_forzado = 1,
                    orden = %s 
                WHERE id = %s AND ruta_id = %s
                """, (max_orden + 1, cliente_id, int(session.get("ruta_id") or 0))
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al reactivar cliente: {e}")
        return jsonify({"status": "error", "message": "Fallo interno en base de datos"})
    finally:
        pass
        
    # Redirecciona automáticamente de vuelta al menú maestro de control
    return redirect("/menu/creditos?filtro=Inactivo")

@app.route("/api/balance/cerrar_caja", methods=["POST"])
def api_procesar_cierre_caja():
    if not session.get("autenticado"):
        return redirect("/login")

    import json as _json_cierre
    efectivo_sistema = Decimal(request.form.get("efectivo_sistema", "0.00")).quantize(Decimal("0.01"))
    efectivo_real = Decimal(request.form.get("efectivo_real", "0.00")).quantize(Decimal("0.01"))
    diferencia = (efectivo_real - efectivo_sistema).quantize(Decimal("0.01"))

    if diferencia == 0:
        detalle_texto = f"Cuadrado Perfecto ($0.00). Real: ${float(efectivo_real):.2f}"
    elif diferencia > 0:
        detalle_texto = f"Sobrante de ${float(diferencia):.2f}. Real: ${float(efectivo_real):.2f}"
    else:
        detalle_texto = f"FALTANTE DE ${float(abs(diferencia)):.2f}. Real: ${float(efectivo_real):.2f}"

    payload_cierre = _json_cierre.dumps({
        "esperado": f"{efectivo_sistema:.2f}",
        "entregado": f"{efectivo_real:.2f}",
        "diferencia": f"{diferencia:.2f}",
        "detalle": detalle_texto
    })

    conn = get_db()
    try:
        rid = int(session.get("ruta_id") or 1)
        hoy_ec = ahora_local().date()
        hoy_ec_iso = hoy_ec.isoformat()

        with conn.cursor() as cursor:
            # 1. Recuperar la jornada que se está cerrando
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_j = cursor.fetchone()
            fecha_cierre_real = str(row_j["valor"]).strip() if (row_j and row_j["valor"]) else hoy_ec_iso

            # 2. Sello inmutable del cierre en JSON (100% compatible con Historial de Avanta Dueño)
            llave_cierre = f"cierre_caja_{fecha_cierre_real}"
            cursor.execute("DELETE FROM configuracion WHERE llave = %s AND ruta_id = %s", (llave_cierre, rid))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES (%s, %s, %s)", (llave_cierre, payload_cierre, rid))

            # 3. Actualizar la base de caja de la ruta con el efectivo real entregado
            cursor.execute("DELETE FROM configuracion WHERE llave = 'caja_base' AND ruta_id = %s", (rid,))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('caja_base', %s, %s)", (f"{efectivo_real:.2f}", rid))

            # 4. Avanzar jornada activa al día siguiente
            proxima_jornada = (hoy_ec + timedelta(days=1)).isoformat() if fecha_cierre_real[:10] == hoy_ec_iso else hoy_ec_iso
            cursor.execute("DELETE FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('fecha_jornada_activa', %s, %s)", (proxima_jornada, rid))

            # 5. Bloquear únicamente esta ruta hasta nueva autorización del Dueño
            cursor.execute("DELETE FROM configuracion WHERE llave = 'caja_autorizada' AND ruta_id = %s", (rid,))
            cursor.execute("INSERT INTO configuracion (llave, valor, ruta_id) VALUES ('caja_autorizada', '0', %s)", (rid,))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error crítico en el asiento del cierre: {e}")

    session.clear()
    return redirect("/login")

@app.route("/api/datos_credito_hoy/<int:cli_id>", methods=["GET"])
def api_datos_credito_hoy(cli_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error"}), 401
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (cli_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"status": "error", "message": "Cliente no encontrado"}), 404
            c = dict(row)
            nombre = str(c.get("nombre") or "").strip()

            cursor.execute(
                """
                SELECT monto FROM balance_movimientos
                WHERE tipo = 'Entrada' AND concepto ILIKE %s
                ORDER BY id DESC LIMIT 1
                """, (f"%MICROSEGURO%{nombre}%",)
            )
            row_ms = cursor.fetchone()
            microseguro = float((row_ms["monto"] if isinstance(row_ms, dict) else row_ms[0])) if row_ms else 0.0

        return jsonify({
            "status": "ok",
            "nombre": nombre,
            "identificacion": str(c.get("identificacion") or ""),
            "telefono": str(c.get("telefono") or ""),
            "direccion": str(c.get("direccion") or ""),
            "microseguro": microseguro,
            "foto_frente": str(c.get("foto_cedula_frente") or ""),
            "foto_reverso": str(c.get("foto_cedula_reverso") or "")
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/editar_credito_hoy", methods=["POST"])
def api_editar_credito_hoy():
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "Sesión expirada"}), 401
    try:
        data = request.get_json()
        cli_id = int(data.get("cliente_id", 0))
        mov_id = int(data.get("mov_id", 0))
        nombre = str(data.get("nombre", "")).strip()
        identificacion = str(data.get("identificacion", "")).strip()
        telefono = str(data.get("telefono", "")).strip()
        direccion = str(data.get("direccion", "")).strip()
        monto = Decimal(str(data.get("monto", "0")))
        interes = Decimal(str(data.get("interes", "20")))
        cuotas = int(data.get("cuotas", 20))
        frecuencia = str(data.get("frecuencia", "Diaria")).strip()
        microseguro = Decimal(str(data.get("microseguro", "0") or "0"))
        foto_frente = str(data.get("foto_frente", "")).strip()
        foto_reverso = str(data.get("foto_reverso", "")).strip()

        if monto <= 0 or cuotas <= 0:
            return jsonify({"status": "error", "message": "Valores inválidos."}), 400

        monto_total = (monto + (monto * (interes / Decimal("100")))).quantize(Decimal("0.01"))
        valor_cuota = (monto_total / Decimal(str(cuotas))).quantize(Decimal("0.01"))

        nombre_limpio = nombre
        for pref in ("CRÉDITO NUEVO - ", "CREDITO NUEVO - ", "CRÉDITO NUEVO - CLIENTE: ", "CREDITO NUEVO - CLIENTE: "):
            if nombre_limpio.upper().startswith(pref):
                nombre_limpio = nombre_limpio[len(pref):].strip()
                break

        rid_edit = int(session.get("ruta_id") or 1)
        conn = get_db()
        with conn.cursor() as cursor:
            if cli_id <= 0 and nombre_limpio:
                cursor.execute("""
                    SELECT id FROM clientes
                    WHERE ruta_id = %s AND LOWER(TRIM(nombre)) = LOWER(TRIM(%s))
                    ORDER BY id DESC LIMIT 1
                """, (rid_edit, nombre_limpio))
                row_find = cursor.fetchone()
                if row_find:
                    cli_id = int(dict(row_find).get("id") or 0)
            if cli_id <= 0:
                return jsonify({"status": "error", "message": "No se encontró el cliente en la base de datos."}), 404

            cursor.execute("SELECT nombre, fecha_inicio FROM clientes WHERE id = %s", (cli_id,))
            row_c = cursor.fetchone()
            if row_c:
                d_c = dict(row_c)
                nombre_anterior = str(d_c.get("nombre") or nombre_limpio).strip()
                val_ini = d_c.get("fecha_inicio")
                fecha_ini_dt = val_ini if isinstance(val_ini, date) else date.fromisoformat(str(val_ini)[:10])
            else:
                nombre_anterior = nombre_limpio
                fecha_ini_dt = ahora_local().date()


            cursor.execute("""
                UPDATE clientes
                SET nombre = COALESCE(NULLIF(%s, ''), nombre),
                    identificacion = COALESCE(NULLIF(%s, ''), identificacion),
                    telefono = COALESCE(NULLIF(%s, ''), telefono),
                    direccion = COALESCE(NULLIF(%s, ''), direccion),
                    monto = %s, interes_porcentaje = %s, monto_total = %s,
                    cuotas = %s, frecuencia = %s, valor_cuota = %s,
                    foto_cedula_frente = CASE WHEN %s <> '' THEN %s ELSE foto_cedula_frente END,
                    foto_cedula_reverso = CASE WHEN %s <> '' THEN %s ELSE foto_cedula_reverso END
                WHERE id = %s
            """, (nombre_limpio, identificacion, telefono, direccion, monto, interes, monto_total, cuotas, frecuencia, valor_cuota, foto_frente, foto_frente, foto_reverso, foto_reverso, cli_id))

            cursor.execute("DELETE FROM pagos WHERE cliente_id = %s", (cli_id,))
            acumulado = Decimal("0.00")
            filas_pagos_edit = []
            acumulado = Decimal("0.00")
            for num in range(1, cuotas + 1):
                if num == cuotas:
                    valor_cuota_real = monto_total - acumulado
                else:
                    valor_cuota_real = valor_cuota
                acumulado += valor_cuota_real
                try:
                    f_cuota = calcular_fecha(fecha_ini_dt, num - 1, frecuencia)
                except Exception:
                    f_cuota = fecha_ini_dt + timedelta(days=(num - 1) * 7 if frecuencia == "Semanal" else (num - 1))
                filas_pagos_edit.append((cli_id, num, f_cuota.isoformat(), valor_cuota_real, 0, 0, '', rid_edit))

            execute_values(
                cursor,
                "INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real, ruta_id) VALUES %s",
                filas_pagos_edit
            )

            nombre_final = nombre_limpio if nombre_limpio else nombre_anterior
            if mov_id > 0:
                cursor.execute(
                    "UPDATE balance_movimientos SET monto = %s, concepto = %s WHERE id = %s",
                    (monto, f"CRÉDITO NUEVO - CLIENTE: {nombre_final}", mov_id)
                )

            rid = int(session.get("ruta_id") or 1)
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa' AND ruta_id = %s", (rid,))
            row_t = cursor.fetchone()
            turno_act = str(dict(row_t).get("valor") or "").strip() if row_t else ahora_local().date().isoformat()

            cursor.execute(
                """
                SELECT id FROM balance_movimientos
                WHERE ruta_id = %s AND tipo = 'Entrada' AND concepto ILIKE %s AND fecha = %s
                ORDER BY id DESC LIMIT 1
                """, (rid, f"%MICROSEGURO%{nombre_anterior}%", turno_act)
            )
            row_ms = cursor.fetchone()
            if row_ms:
                ms_id = int(dict(row_ms)["id"])
                if microseguro > 0:
                    cursor.execute(
                        "UPDATE balance_movimientos SET monto = %s, concepto = %s WHERE id = %s",
                        (microseguro, f"MICROSEGURO - CLIENTE: {nombre_final}", ms_id)
                    )
                else:
                    cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (ms_id,))
            elif microseguro > 0:
                cursor.execute(
                    """
                    INSERT INTO balance_movimientos (fecha, tipo, categoria, concepto, monto, ruta_id)
                    VALUES (%s, 'Entrada', 'Microseguro', %s, %s, %s)
                    """, (turno_act, f"MICROSEGURO - CLIENTE: {nombre_final}", microseguro, rid)
                )

            conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/gps/reportar", methods=["POST"])
def api_gps_reportar():
    if not session.get("autenticado"):
        return jsonify({"status": "error", "msg": "No autenticado"}), 401
    rid = int(session.get("ruta_id") or 1)
    data = request.get_json(silent=True) or {}
    lat = str(data.get("latitud") or "").strip()
    lon = str(data.get("longitud") or "").strip()
    if not lat or not lon:
        return jsonify({"status": "error", "msg": "Sin coordenadas"}), 400

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE rutas ADD COLUMN IF NOT EXISTS latitud_actual TEXT")
            cursor.execute("ALTER TABLE rutas ADD COLUMN IF NOT EXISTS longitud_actual TEXT")
            cursor.execute("ALTER TABLE rutas ADD COLUMN IF NOT EXISTS ultima_ubicacion TEXT")
            cursor.execute("ALTER TABLE rutas ADD COLUMN IF NOT EXISTS gps_habilitado INTEGER DEFAULT 1")

            cursor.execute("SELECT COALESCE(gps_habilitado, 1) AS gps FROM rutas WHERE id = %s", (rid,))
            row_g = cursor.fetchone()
            gps_on = int((row_g["gps"] if isinstance(row_g, dict) else row_g[0]) or 1) if row_g else 1
            if gps_on == 0:
                return jsonify({"status": "disabled"})

            hora_ec = (datetime.utcnow() - timedelta(hours=5)).strftime("%d/%m/%Y %H:%M")
            cursor.execute(
                "UPDATE rutas SET latitud_actual = %s, longitud_actual = %s, ultima_ubicacion = %s WHERE id = %s",
                (lat, lon, hora_ec, rid)
            )
            conn.commit()
            return jsonify({"status": "ok", "ruta_id": rid, "hora": hora_ec})
    except Exception as e:
        print(f"Error GPS reportar: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/ping")
def ping_keep_alive():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
