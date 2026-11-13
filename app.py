import base64
import os
import urllib.parse
from decimal import Decimal
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, session, send_file, g
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2.pool import SimpleConnectionPool
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "avanta_secret_key_1.3_2026")

# 🛡️ SISTEMA SEGURO: Lee la credencial desde el contenedor secreto de Render sin exponerla en código
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("Error crítico: DATABASE_URL no está configurada en las variables de entorno.")

# 🔌 FIJADO: Pool de conexiones para evitar caídas de base de datos en Render/Neon
db_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=5,
    dsn=DATABASE_URL,
    cursor_factory=DictCursor
)

def get_db():
    """🛡️ FIJADO: Registra, valida y repara la conexión en el contexto 'g' de Flask para erradicar Errores 500"""
    if 'db_conn' not in g:
        g.db_conn = db_pool.getconn()
    else:
        # Probamos si la conexión del pool sigue viva
        try:
            with g.db_conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            # Si Neon cerró el socket por inactividad, la liberamos forzadamente y pedimos otra limpia
            try:
                db_pool.putconn(g.db_conn)
            except Exception:
                pass
            g.db_conn = db_pool.getconn()
            
    return g.db_conn

def release_db(conn):
    """Debe quedar vacío, el context manager se encarga abajo"""
    pass

@app.teardown_appcontext
def shutdown_session(error_context=None):
    """
    🛡️ FIJADO AUTOMÁTICO: Saca la conexión de 'g' y la libera de verdad
    al terminar cada petición HTTP, evitando colgar el inicio de sesión.
    """
    conn = g.pop('db_conn', None)
    if conn is not None:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            print(f"Error al liberar conexión en el pool: {e}")

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
            
            # 🔐 FIJADO: Encriptación segura de contraseña administrativa con hash seguro
            password_encriptada = generate_password_hash("1234")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('usuario', 'admin') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('clave', %s) ON CONFLICT DO NOTHING", (password_encriptada,))
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('empresa_nombre', 'AVANTA PAGOS') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('empresa_telefono', '593991234567') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('ticket_encabezado', '¡BIENVENIDO A AVANTA PAGOS!') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('caja_base', '100.00') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('balance_estado', 'Sin cerrar') ON CONFLICT DO NOTHING")
            # 🔐 FIJADO: Inicialización de la llave de seguridad para la autorización de jornada
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('caja_autorizada', '0') ON CONFLICT DO NOTHING")
        conn.commit()
    finally:
        release_db(conn)


with app.app_context():
    init_db()
    init_db_contable()
def obtener_clientes_ruta_hoy():
    hoy_str = date.today().isoformat()
    hoy_date = date.today()
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
        * { box-sizing: border-box; font-family: 'Inter', system-ui, -apple-system, sans-serif; margin: 0; padding: 0; }
        body { background: linear-gradient(135deg, #0b1329 0%, #1c2541 100%); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 16px; }
        .login-card { background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); padding: 36px 24px; border-radius: 24px; width: 100%; max-width: 380px; text-align: center; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.4); }
        .logo-container { margin-bottom: 20px; }
        .logo-icon { font-size: 32px; background: linear-gradient(135deg, #38bdf8, #0369a1); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 800; display: inline-block; letter-spacing: -0.5px; }
        .logo-subtitle { color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-top: 4px; }
        h2 { color: white; font-size: 18px; font-weight: 700; margin: 20px 0; text-align: center; letter-spacing: 0.3px; }
        .input-group { text-align: left; margin-bottom: 18px; }
        label { color: #cbd5e1; font-size: 11px; font-weight: 700; text-transform: uppercase; margin-bottom: 6px; display: block; letter-spacing: 0.5px; }
        input { width: 100%; padding: 14px 16px; background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; color: white; font-size: 14px; font-weight: 500; outline: none; display: block; margin-top: 4px; transition: all 0.2s ease-in-out; }
        input::placeholder { color: #64748b; }
        input:focus { border-color: #0ea5e9; background: rgba(15, 23, 42, 0.8); box-shadow: 0 0 0 4px rgba(14, 165, 233, 0.15); }
        .btn-access { background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; font-weight: 700; font-size: 14px; border: none; padding: 14px; border-radius: 12px; width: 100%; cursor: pointer; margin-top: 14px; box-shadow: 0 4px 12px rgba(14, 165, 233, 0.3); display: block; transition: all 0.2s ease-in-out; }
        .btn-access:hover { background: linear-gradient(135deg, #0284c7, #0369a1); box-shadow: 0 6px 16px rgba(14, 165, 233, 0.45); transform: translateY(-1px); }
        .btn-access:active { transform: translateY(0); }
        .version-text { color: #475569; font-size: 11px; font-weight: 600; margin-top: 24px; }
        .error-msg { background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; color: #fca5a5; padding: 10px; border-radius: 8px; font-size: 12px; margin-bottom: 16px; font-weight: 600; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo-container">
            <div class="logo-icon">🌐 AVANTA PAGOS</div>
            <div class="logo-subtitle">SISTEMA PRO v1.3</div>
        </div>
        <h2>Control de Acceso</h2>
        {% if error %} <div class="error-msg">⚠️ {{ error }}</div> {% endif %}
        <form action="/login" method="POST" onsubmit="setTimeout(() => { const b = document.getElementById('btnLoginSubmit'); if(b){ b.disabled = true; b.innerText = '⌛ Autenticando...'; b.style.opacity = '0.7'; } }, 10);">
            <div class="input-group">
                <label>Usuario Comercial</label>
                <input type="text" name="usuario" required placeholder="Ingresa tu usuario">
            </div>
            <div class="input-group">
                <label>Contraseña</label>
                <input type="password" name="clave" required placeholder="••••••••">
            </div>
            <button type="submit" id="btnLoginSubmit" class="btn-access">Acceder al Sistema</button>
        </form>
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

            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if es_mora else 0 }}">
                <div class="card-izquierda">
                    <div class="circulo-frecuencia {% if not es_mora %}al-dia{% else %}mora-leve{% endif %}">
                        {{ 'D' if c.frecuencia == 'Diaria' else 'S' }}
                    </div>
                    <div class="info-bloque-texto">
                        <div class="id-alias">{{ c.id }} {{ c.referencia or 'Comercio' }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <div class="grid-metricas-premium">
                            <div class="metric-col"><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${{ "%.2f"|format(c.valor_cuota) }}</span></div>
                            <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{{ cuotas_pagadas }} / {{ c.cuotas }}.0</span></div>
                            <div class="metric-col"><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                        </div>
                    <!-- 🛠️ Fila Inferior de Utilidades (Foto, Check de Cuota y Saldo) -->
                    <div class="fila-utilidades-premium">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero if cuota_pendiente else 1 }}, {{ (cuota_pendiente.valor - cuota_pendiente.valor_pagado) if cuota_pendiente else 0 }}, {{ c.valor_cuota }}); alert('📸 Adjunta el comprobante para la cuota de {{ c.nombre }}');">
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
    <div class="section-header-title"><i class="fa-solid fa-users"></i> DIRECTORIO DE CLIENTES</div>
    
    <div style="padding: 0 4px 12px 4px;">
        <input type="text" id="buscadorClientesDir" onkeyup="filtrarDirectorioClientes()" 
               placeholder="🔍 Buscar cliente por nombre o referencia..." 
               style="width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #cbd5e1; font-size: 14px; outline: none;">
    </div>

    <div id="listaDirectorioClientes" style="display: flex; flex-direction: column; gap: 8px; padding: 0 4px 70px 4px;">
        {% for c in lista_clientes %}
        <div class="card item-cliente-dir" data-filtro="{{ c.nombre|lower }} {{ (c.referencia or '')|lower }}"
             onclick="navegarRuta('/menu/cliente/{{ c.id }}')"
             style="padding: 14px; margin: 0; background: white; border-radius: 10px; border: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center; cursor: pointer;">
            <div style="text-align: left;">
                <span style="font-weight: 800; color: #0f2b5c; font-size: 14px; display: block;">{{ c.nombre }}</span>
                <span style="font-size: 11px; color: #64748b;">{{ c.referencia or 'Sin referencia' }} • 📱 {{ c.telefono or 'N/A' }}</span>
            </div>
            <span style="color: #94a3b8; font-weight: bold; font-size: 16px;">❯</span>
        </div>
        {% else %}
        <div style="text-align: center; color: #94a3b8; padding: 20px;">No hay clientes registrados.</div>
        {% endfor %}
    </div>

    <div style="position: fixed; bottom: 15px; left: 50%; transform: translateX(-50%); width: 92%; max-width: 460px; z-index: 90;">
        <button type="button" onclick="navegarRuta('/nuevo')"
                style="width: 100%; padding: 14px; background: #0f2b5c; color: white; border: none; border-radius: 12px; font-weight: 800; font-size: 14px; box-shadow: 0 4px 12px rgba(15,43,92,0.3); cursor: pointer;">
            + Nuevo Cliente / Crédito
        </button>
    </div>

{% elif vista == 'perfil_cliente' %}
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding: 0 4px;">
        <button type="button" onclick="navegarRuta('/menu/clientes')" style="background: #e2e8f0; border: none; padding: 8px 12px; border-radius: 8px; font-weight: bold; color: #1e293b; cursor: pointer;">❮ Volver</button>
        <span style="font-weight: 800; color: #0f2b5c; font-size: 14px;">EXPEDIENTE DEL CLIENTE</span>
        <div style="width: 60px;"></div>
    </div>

    {% if cliente %}
    <div class="card" style="padding: 12px; margin-bottom: 12px; background: white; border-radius: 12px; border: 1px solid #e2e8f0; text-align: left;">
        <div style="font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; margin-bottom: 8px;">Historial de Créditos</div>
        {% for cr in creditos %}
        <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #f1f5f9;">
            <div>
                <span style="font-size: 12px; font-weight: bold; color: #0f2b5c;">Inicio: {{ cr.fecha_inicio }}</span>
                <span style="display: block; font-size: 11px; color: {{ '#10b981' if cr.saldo <= 0 else '#dc2626' }}; font-weight: bold;">
                    {{ 'LIQUIDADO' if cr.saldo <= 0 else 'ACTIVO' }}
                </span>
            </div>
            <div style="text-align: right;">
                <span style="font-size: 11px; color: #64748b; display: block;">Total: ${{ "%.2f"|format(cr.monto_total) }}</span>
                <span style="font-size: 13px; font-weight: 800; color: #0f2b5c;">Saldo: ${{ "%.2f"|format(cr.saldo) }}</span>
            </div>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    <div style="padding: 0 4px 12px 4px;">
        {% if cliente and cliente.latitud and cliente.longitud %}
        <button type="button" onclick="abrirGpsCliente('{{ cliente.latitud|default('') }}', '{{ cliente.longitud|default('') }}')"
                style="width: 100%; padding: 12px; background: #10b981; color: white; border: none; border-radius: 10px; font-weight: 800; font-size: 13px; display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer; box-shadow: 0 2px 6px rgba(16,185,129,0.3);">
            📍 Abrir Ubicación Exacta en Google Maps
        </button>
        {% else %}
        <div style="padding: 10px; background: #fef3c7; color: #b45309; border-radius: 10px; font-size: 12px; text-align: center; font-weight: bold;">
            ⚠️ Este cliente no tiene coordenadas GPS guardadas.
        </div>
        {% endif %}
    </div>

    <div class="card" style="padding: 14px; background: white; border-radius: 12px; border: 1px solid #e2e8f0; text-align: left;">
        <div style="margin-bottom: 10px;">
            <label style="font-size: 11px; font-weight: bold; color: #64748b;">Nombre Completo</label>
            <input type="text" id="perf_nombre" value="{{ cliente.nombre if cliente else '' }}" readonly class="input-perfil" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc; font-weight: bold; color: #0f2b5c;">
        </div>

        <div style="margin-bottom: 10px;">
            <label style="font-size: 11px; font-weight: bold; color: #64748b;">Identificación / Cédula</label>
            <input type="text" id="perf_cedula" value="{{ cliente.identificacion if cliente else '' }}" readonly class="input-perfil" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc; font-weight: bold; color: #0f2b5c;">
        </div>

        <div style="margin-bottom: 10px;">
            <label style="font-size: 11px; font-weight: bold; color: #64748b;">Teléfono / WhatsApp</label>
            <input type="text" id="perf_telefono" value="{{ (cliente.telefono or '') if cliente else '' }}" readonly class="input-perfil" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc;">
        </div>

        <div style="margin-bottom: 10px;">
            <label style="font-size: 11px; font-weight: bold; color: #64748b;">Dirección Escrita</label>
            <input type="text" id="perf_direccion" value="{{ (cliente.direccion or '') if cliente else '' }}" readonly class="input-perfil" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc;">
        </div>

        <div style="margin-bottom: 10px;">
            <label style="font-size: 11px; font-weight: bold; color: #64748b;">Referencia / Negocio</label>
            <input type="text" id="perf_referencia" value="{{ (cliente.referencia or '') if cliente else '' }}" readonly class="input-perfil" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc;">
        </div>

        <div style="display: flex; gap: 8px; margin-top: 14px;">
            <button type="button" id="btnEditarPerfil" onclick="toggleEdicionPerfil({{ cliente.id if cliente else 0 }})"
                    style="flex: 1; padding: 12px; background: #f1f5f9; color: #0f2b5c; border: 1px solid #cbd5e1; border-radius: 10px; font-weight: 800; font-size: 13px; cursor: pointer;">
                ✏️ Modificar
            </button>
            <button type="button" onclick="navegarRuta('/mover_renovacion/{{ cliente.id if cliente else 0 }}')"
                    style="flex: 1.3; padding: 12px; background: #0f2b5c; color: white; border: none; border-radius: 10px; font-weight: 800; font-size: 13px; cursor: pointer;">
                💵 Conceder Crédito
            </button>
        </div>
    </div>

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
                        <div class="id-alias">{{ c.id }} — {{ c.referencia or c.nombre }}</div>
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
                                <button type="button" class="btn-accion" onclick="if(confirm('¿Desea reactivar a este cliente y regresarlo a la ruta con su saldo restante?')) window.location.href='/api/clientes/reactivar/{{ c.id }}'" style="background: #10b981; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 11px;">
                                    <i class="fa-solid fa-bolt"></i> Reactivar
                                </button>
                            {% endif %}
                            
                            {% if c.estado == 'Activo' %}
                                <button type="button" class="btn-accion" onclick="if(confirm('¿Mover a Lista Negra?')) window.location.href='/api/clientes/lista_negra/{{ c.id }}'" style="background:#475569; padding: 6px 12px; font-size: 11px;"><i class="fa-solid fa-ban"></i> Bloquear</button>
                            {% endif %}
                            <a href="/api/eliminar_cliente/{{ c.id }}" onclick="return confirm('¿Eliminar de forma permanente?')" class="btn-accion btn-nopagar" style="text-decoration:none; padding: 6px 12px; background:#dc2626; font-size: 11px;"><i class="fa-solid fa-trash-can"></i> Borrar</a>
                            
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
    <link rel="stylesheet" href="https://cloudflare.com">
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
            fetch('/api/revertir_gestion/' + tipo + '/' + idStr)
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'ok') {
                        navegarRuta('/menu/pagos_hoy');
                    } else {
                        alert('⚠️ No se pudo revertir: ' + (data.message || 'Error en el servidor'));
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
    <h3 style="margin-top:0; color:#0f2b5c;">{% if vista == 'renovar' %}🔄 Renovar Crédito a {{ cliente.nombre }}{% else %}👤 Registro de Crédito / Venta{% endif %}</h3>
    <div class="card" style="padding:16px;">
        <form id="formNuevoCredito" action="/api/clientes/guardar" method="POST" onsubmit="enviarCreditoAsincrono(event)">
            {% if vista == 'renovar' %}<input type="hidden" name="cliente_id" value="{{ cliente.id }}">{% endif %}
            <label>Nombre del Cliente</label><input type="text" name="nombre" value="{{ cliente.nombre | default('') }}" required>
            <label>WhatsApp</label><input type="text" name="telefono" value="{{ cliente.telefono | default('') }}" required>
            <label>Identificación / Cédula</label><input type="text" name="identificacion" value="{{ cliente.identificacion | default('') }}">
            <label>Dirección Domiciliaria</label><input type="text" name="direccion" value="{{ cliente.direccion | default('') }}">
            <label>Referencia Local</label><input type="text" name="referencia" value="{{ cliente.referencia | default('') }}">
            <label>Monto Financiado ($)</label><input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            <label>Interés (%)</label>
            <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            
            <label>Cuotas</label>
            <input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            
            <label>Opciones de Microseguro</label>
            <select name="aplicar_seguro" id="calcSeguroAccion" onchange="manejarTipoSeguro()" style="width:100%; padding:10px; border-radius:8px; border:1px solid #cbd5e1; font-size:13px; outline:none; margin-top:4px;">
                <option value="NO">❌ No aplicar seguro (Entrega 100%)</option>
                <option value="2">🛡️ Seguro Básico (2%)</option>
                <option value="4" selected>🛡️ Seguro Premium (4%)</option>
                <option value="MANUAL">✏️ Ingresar Monto Manual</option>
            </select>

            <div id="contenedorSeguroManual" style="display: none; margin-top: 8px;">
                <label>Monto del Seguro ($)</label>
                <input type="number" step="any" id="calcSeguroManualVal" name="microseguro_manual_valor" value="0" oninput="calcularCuota()" style="width:100%; padding:10px; border-radius:8px; border:1px solid #cbd5e1; font-size:13px; outline:none; margin-top:4px;">
            </div>
            
            <div id="seguroDesgloseText" style="font-size:12px; color:#475569; font-weight:700; margin-top:6px;">Monto Seguro: $0.00 | Neto a Entregar: $0.00</div>
            <div id="simulacionText" style="font-weight:bold; color:#0369a1; margin:6px 0;">$0.00 / cuota</div>
            <label>Frecuencia</label>
            <select name="frecuencia"><option value="Diaria">Diaria</option><option value="Semanal">Semanal</option></select>
            <label>Fecha de Inicio</label><input type="date" name="fecha_inicio" value="{{ hoy_str }}" required>
            <input type="hidden" id="latitud_input" name="latitud">
            <input type="hidden" id="longitud_input" name="longitud">
            <div style="margin-bottom: 15px;">
            <button type="button" onclick="obtenerCoordenadasGPS()" style="width: 100%; padding: 12px; background-color: #0284c7; color: white; border: none; border-radius: 8px; font-weight: bold; display: flex; align-items: center; justify-content: center; gap: 8px;">📍 Obtener Ubicación GPS</button>
            <p id="status_gps" style="font-size: 12px; color: #64748b; margin-top: 5px; text-align: center;">Ubicación no registrada aún.</p>
            <!-- ⚡ CORREGIDO: Se cambia 'btn-primary' por 'btn-accion btn-pagar' para heredar el comportamiento de clic prioritario -->
            <button type="submit" id="btnGuardarCredito" class="btn-primary" style="width:100%; padding:12px; margin-top:8px; font-size:13px; font-weight:800; background:#10b981; color:white; border:none; border-radius:8px; cursor:pointer;" onclick="setTimeout(() => { const btn = document.getElementById('btnGuardarCredito'); if(btn) { btn.disabled = true; btn.innerText = '⌛ Guardando...'; } }, 10);"> Guardar e Inicializar Tablas</button>
       </form>
    </div>

{% elif vista == 'balance' or vista == 'gastos' %}
    <div class="section-header-title"><i class="fa-solid fa-scale-balanced"></i> Estado Contable del Balance</div>
    <div class="expected-card" style="border-left: 5px solid #10b981; background:#ecfdf5; padding:12px; border-radius:12px; margin-bottom:12px; text-align:left;">
        <div class="kpi-title" style="color:#065f46;">Estado Actual</div>
        <div class="kpi-val" style="color:#065f46; font-size:14px;">Sin Cerrar | Caja Base: ${{ "%.2f"|format(caja_base) }}</div>
    </div>
    
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px;">
        <div class="kpi-card" style="border-left:4px solid #10b981; background:white; padding:10px; border-radius:8px;">
            <div class="kpi-title">Inversión / Entradas</div>
            <div class="kpi-val" style="color:#10b981;">+${{ "%.2f"|format(entradas_totales) }}</div>
        </div>
        <div class="kpi-card" style="border-left:4px solid #ef4444; background:white; padding:10px; border-radius:8px;">
            <div class="kpi-title">Salidas / Gastos</div>
            <div class="kpi-val" style="color:#ef4444;">-${{ "%.2f"|format(salidas_totales) }}</div>
        </div>
    </div>

    <div class="card" style="background:#0f2b5c; color:white; text-align:center; padding:12px; margin-bottom:12px;">
        <div class="kpi-title" style="color:white; opacity:0.8;">Efectivo Líquido Neto</div>
        <div class="kpi-val" style="color:white; font-size:20px; margin-top:2px;">${{ "%.2f"|format(efectivo_neto) }}</div>
    </div>

        <!-- 🔒 CONTROL INTEGRADO DE CIERRE DE JORNADA LABORAL PREMIUM -->
    <div class="card" style="text-align: left; padding: 14px; border-radius: 12px; margin-bottom: 14px; background: #ffffff; box-shadow: 0 2px 4px rgba(0,0,0,0.02); font-family: sans-serif;">
        <h4 style="font-size: 13px; color: #0f2b5c; margin: 0 0 8px 0; font-weight: 800;"><i class="fa-solid fa-lock-open"></i> Cierre de Jornada Diaria</h4>
        <p style="font-size: 11px; color: #64748b; margin-bottom: 12px; line-height: 1.4; margin-top: 0;">
            Al finalizar el día, ingresa el dinero físico real que tienes en tu bolso o cartera para realizar la auditoría y cerrar la jornada de cobranza de forma profesional. El sistema se cerrará automáticamente.
        </p>

        {% if balance_estado != 'Abierta' %}
            <div style="background: #ecfdf5; border: 1px solid #10b981; color: #065f46; padding: 10px; border-radius: 8px; font-size: 12px; font-weight: bold; text-align: center;">
                🔒 CAJA CERRADA CON ÉXITO:<br>{{ balance_estado }}
            </div>
        {% else %}
            <form action="/api/balance/cerrar_caja" method="POST" style="display: flex; flex-direction: column; gap: 8px;">
                <input type="hidden" name="efectivo_sistema" value="{{ efectivo_neto }}">
                <label style="font-size: 11px; font-weight: bold; color: #475569; display: block; margin-bottom: 4px;">Efectivo Físico Real en Mano ($)</label>
                <input type="number" step="any" name="efectivo_real" required placeholder="Ej: {{ efectivo_neto }}" style="width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                <button type="submit" class="btn-accion btn-pagar" style="width: 100%; padding: 12px; font-weight: bold; background: #e11d48; color: white; border: none; border-radius: 8px; cursor: pointer; box-shadow: 0 3px 6px rgba(225, 29, 72, 0.2); font-size: 12px;"><i class="fa-solid fa-power-off"></i> Sellar y Cerrar Caja de Hoy</button>
            </form>
        {% endif %}
    </div>

    <div class="card" style="padding:16px;">
        <div class="kpi-title" style="margin-bottom:8px;">Registrar Movimiento de Flujo</div>
        <!-- 🔑 FIJADO: Envío tradicional cancelado. Delegado al script asíncrono controlado procesarYEnviarGasto -->
        <form id="formBalance" method="POST" action="/api/balance/guardar_movimiento" onsubmit="procesarYEnviarGasto(event)">
            <label>Tipo de Flujo</label>
            <select name="tipo_mov" id="tipo_mov" required>
                <option value="Entrada">📥 Entrada / Inversión Capital</option>
                <option value="Salida" selected>📤 Salida / Registro de Gasto</option>
            </select>
            
            <label>Categoría del Movimiento</label>
            <select name="categoria_mov" id="categoria_mov">
                <option value="Inversión">💰 Inversión de Capital</option>
                <option value="Gasolina">⛽ Gasolina</option>
                <option value="Almuerzo">🍔 Almuerzo</option>
                <option value="Mantenimiento">🛠️ Mantenimiento Vehículo</option>
                <option value="Viáticos">🎒 Viáticos de Ruta</option>
                <option value="Sueldo">💵 Comisión</option>
                <option value="Otros" selected>📦 Otros</option>
            </select>
            
            <label>Descripción / Detalle</label>
            <input type="text" name="concepto_mov" id="concepto_mov" placeholder="Ej: Compra de repuestos de moto" required>
            
            <label>Monto ($)</label>
            <input type="number" step="any" name="monto_mov" id="monto_mov" placeholder="Valor en dinero" required>
            
            <label>📸 Foto Factura / Comprobante</label>
            <input type="file" id="foto_mov" accept="image/*" capture="environment" style="border:none; padding:4px 0;" onchange="procesarImagenEnCaliente()">
            
            <input type="hidden" id="foto_comprimida_b64" name="foto_comprimida_b64">
            
        <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:12px; margin-top:10px;">Guardar Registro</button>
        </form>
    </div>

    <div class="card" style="padding:16px; text-align:left;">
        <h4 style="font-size:13px; color:#0f2b5c; margin-bottom:10px;">📋 Historial de Movimientos de Hoy</h4>
        {% if gastos_list %}
            {% for g in gastos_list %}
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding:8px 0; font-size:12px;">
                    <div style="flex:1;">
                        <b>[{{ g.categoria }}]</b> {{ g.concepto }}
                        <span style="color: {% if g.tipo == 'Entrada' %}#10b981{% else %}#ef4444{% endif %}; font-weight:bold; margin-left:6px;">
                                {% if g.tipo == 'Entrada' %}+{% else %}-{% endif %}${{ "%.2f"|format(g.monto) }}
                        </span>
                    </div>
                    <div style="display:flex; align-items:center; gap:8px;">
                        {% if g.comprobante and g.comprobante != "None" and g.comprobante != "" %}
                            <button type="button" onclick="cargarYVerFoto({{ g.id }})" style="padding:4px 8px; font-size:11px; background:#00a8cc; color:white; border:none; border-radius:6px; cursor:pointer;">📷 Ver</button>
                        {% endif %}
                        {# 🛡️ BLINDAJE CONTABLE: Oculta el borrado si es un Microseguro o Recaudación automática de cuotas #}
                        {% if "MICROSEGURO" not in g.concepto and "RECAUDACIÓN DE CUOTAS" not in g.concepto %}
                            <a href="/api/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar Gasto?')" style="color:#ef4444; text-decoration:none; font-weight:bold; font-size:14px; margin-left:4px;">🗑️</a>
                        {% endif %}
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center;">No hay flujos asentados hoy.</p>
        {% endif %}
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
                {{ (nombre[:2] if nombre else 'AV')|upper }}
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
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    Nombre Completo
                </label>
                <input type="text" id="perfilNombre" value="{{ nombre }}" placeholder="Ej: Jhon Ramírez" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; background:#f8fafc; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="7" y1="8" x2="17" y2="8"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="7" y1="16" x2="13" y2="16"/></svg>
                    Cédula de Identidad
                </label>
                <input type="text" id="perfilCedula" value="{{ cedula }}" placeholder="Ej: 1314792449" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; background:#f8fafc; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
                    Número de Celular
                </label>
                <input type="text" id="perfilCelular" value="{{ celular }}" placeholder="Ej: 0989329300" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; background:#f8fafc; box-sizing:border-box;">
            </div>

            <div style="margin-bottom:20px;">
                <label style="font-size:11px; font-weight:700; color:#64748b; display:flex; align-items:center; gap:6px; margin-bottom:6px; text-transform:uppercase;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                    Correo Electrónico
                </label>
                <input type="email" id="perfilCorreo" value="{{ correo }}" placeholder="correo@empresa.com" style="width:100%; padding:12px 14px; border:1px solid #cbd5e1; border-radius:10px; font-size:14px; font-weight:600; color:#0f172a; background:#f8fafc; box-sizing:border-box;">
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
    <link href="https://googleapis.com" rel="stylesheet">
    <link rel="stylesheet" href="https://cloudflare.com" crossorigin="anonymous">
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
    <div class="navbar">
        <button class="btn-menu btn-nav-icon" onclick="toggleDrawer()"><i class="fa-solid fa-bars"></i> Menú</button>
        <div class="nav-title">🌐 AVANTA PAGOS</div>
        <a href="javascript:void(0)" onclick="navegarRuta('/nuevo')" class="btn-nav-icon" style="background:#00a8cc;"><i class="fa-solid fa-plus"></i> Nuevo</a>
    </div>

    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
        <div class="drawer" id="drawer" style="background: #ffffff; font-family: 'Segoe UI', system-ui, sans-serif; box-shadow: 4px 0 20px rgba(0,0,0,0.08); padding: 0;">
        <div class="drawer-header" style="background: linear-gradient(135deg, #0f2b5c, #1e40af); padding: 16px 14px; text-align: left; color: white;">
            <div class="drawer-logo" style="font-size: 16px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px;">🌐 AVANTA PAGOS v1.3</div>
            <div style="font-size: 11px; opacity: 0.8; margin-top: 4px;"><i class="fa-solid fa-user-shield"></i> {{ session.get('usuario', 'Cobrador') }}</div>
        </div>
        
        <ul class="drawer-menu" style="list-style: none; padding: 14px 10px; margin: 0; display: flex; flex-direction: column; gap: 7px;">
            <!-- 👤 MI PERFIL -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/perfil')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #3b82f6;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">👤</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Mi Perfil</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 👥 CLIENTES -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/clientes')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #2563eb;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">👥</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Clientes</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 💳 CRÉDITOS -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/creditos')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #0284c7;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">💳</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Créditos</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 📍 RUTA -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/ruta_orden')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #4f46e5;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">📍</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Ruta</span>
                    </div>
                    <span style="background: #e0e7ff; color: #4f46e5; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px;">Organizar</span>
                </a>
            </li>

            <!-- 📋 LISTADOS -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/listados')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #06b6d4;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">📋</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Listados</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 📅 BALANCE -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/balance')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #f97316;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">📅</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Balance</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 📥 AGREGAR A LISTA -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/agregar_lista')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #64748b;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">📥</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Agregar a lista</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 🔰 SALVAR DATOS -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/salvar_datos')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #10b981;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">🔰</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Salvar datos</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- 🔌 BLUETOOTH -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/bluetooth')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #14b8a6;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">🔌</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Opciones Bluetooth</span>
                    </div>
                    <span style="background: #ccfbf1; color: #14b8a6; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px;">📡 58mm</span>
                </a>
            </li>

            <!-- 🔧 ACCIONES MAESTRO / CONFIGURACIÓN -->
            <li style="margin: 0;">
                <a href="javascript:void(0)" onclick="navegarRuta('/menu/configuracion')" style="display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #f8fafc; border-radius: 10px; text-decoration: none; border-left: 4px solid #6366f1;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 15px;">🔧</span>
                        <span style="font-size: 13px; font-weight: 700; color: #334155;">Acciones Maestro</span>
                    </div>
                    <i class="fa-solid fa-chevron-right" style="font-size: 9px; color: #cbd5e1;"></i>
                </a>
            </li>

            <!-- ⏻ CERRAR SESIÓN CORREGIDO -->
            <li style="margin: 10px 0 0 0; padding-top: 8px; border-top: 1px solid #f1f5f9;">
                <a href="/logout" style="display: flex; align-items: center; justify-content: center; gap: 10px; padding: 12px; background: #e11d48; color: white; text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 13px; box-shadow: 0 4px 6px rgba(225, 29, 72, 0.12);">
                    <svg xmlns="http://w3.org" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block;">
                        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
                        <polyline points="16 17 21 12 16 7"></polyline>
                        <line x1="21" y1="12" x2="9" y2="12"></line>
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

    <div class="modal" id="modalWs">
        <div class="modal-content">
            <h3 style="color:#10b981;"><i class="fa-solid fa-circle-check"></i> Pago Procesado</h3>
            <p id="modalMsg" style="margin:10px 0;"></p>
            <a href="javascript:void(0)" id="modalWsBtn" target="_blank" class="btn-ws"><i class="fa-brands fa-whatsapp"></i> WhatsApp Nativo</a>
            <button onclick="imprimirTicket()" class="btn-primary" style="background:#0f2b5c; margin-top:8px; color:white; border:none; padding:10px; border-radius:8px; width:100%; font-weight:bold; cursor:pointer;"><i class="fa-solid fa-print"></i> Imprimir Recibo</button>
            <button onclick="window.location.reload();" class="btn-modal-close">Continuar</button>
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
            <div style="display:flex; justify-content:space-between;"><span>Días de mora</span><b id="gpDiasMora" style="color:#0f172a;">0</b></div>
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
            <label style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gpChkCastigo" style="opacity:0; width:0; height:0;">
                <span onclick="var c=document.getElementById('gpChkCastigo'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </label>
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
            <label style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gpChkPagarTotal" style="opacity:0; width:0; height:0;">
                <span id="gpSwitchTotalVisual" onclick="togglePagarTotalGP()" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </label>
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
            <div style="display:flex; justify-content:space-between;"><span>Días de mora</span><b id="gnpDiasMora" style="color:#0f172a;">0</b></div>
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
            <label style="position:relative; display:inline-block; width:46px; height:26px;">
                <input type="checkbox" id="gnpChkCastigo" style="opacity:0; width:0; height:0;">
                <span onclick="var c=document.getElementById('gnpChkCastigo'); c.checked=!c.checked; this.style.background=c.checked?'#16a34a':'#cbd5e1'; this.firstElementChild.style.transform=c.checked?'translateX(20px)':'translateX(0)';" style="position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#cbd5e1; border-radius:26px; transition:.2s;">
                    <span style="position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,0.3);"></span>
                </span>
            </label>
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

<script>
function toggleDrawer() {
    document.getElementById('drawer').classList.toggle('active');
    document.getElementById('drawerOverlay').classList.toggle('active');
}
function navegarRuta(url) {
    if (document.getElementById('drawerOverlay').classList.contains('active')) toggleDrawer();
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(res => res.text())
        .then(html => {
            document.getElementById('mainContent').innerHTML = html;
                    if (url === '/nuevo' || url.startsWith('/mover_renovacion')) activarGpsNativo();
        });
}
function activarGpsNativo() {
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(function(position) {
            if(document.getElementById('input_latitud')) {
                document.getElementById('input_latitud').value = position.coords.latitude;
                document.getElementById('input_longitud').value = position.coords.longitude;
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

function abrirModalAbono(clienteId, numCuota, pendiente, valorCuota, diasMora = 0) {
    diasMoraGlobalTemporal = parseInt(diasMora) || 0;
    document.getElementById('abonoClienteId').value = clienteId;
    document.getElementById('abonoNumCuota').value = numCuota;

    const card = document.getElementById('cliente-card-' + clienteId);
    const nombre = card ? (card.querySelector('.nombre-real')?.innerText || 'Cliente') : 'Cliente #' + clienteId;
    const alias = card ? (card.querySelector('.id-alias')?.innerText || '-') : '-';
    const cedula = card ? (card.getAttribute('data-cedula') || 'N/A') : 'N/A';
    const saldoTxt = card ? (card.querySelector('.badge-saldo-premium b')?.innerText || ('$' + pendiente.toFixed(2))) : ('$' + pendiente.toFixed(2));
    const saldoNum = parseFloat(saldoTxt.replace(/[^0-9.]/g, '')) || parseFloat(pendiente) || 0;
    const vCuota = parseFloat(valorCuota) || parseFloat(pendiente) || 0;
    const maxCuotas = vCuota > 0 ? Math.max(1, Math.ceil(saldoNum / vCuota)) : 1;

    document.getElementById('gpIdCliente').innerText = clienteId;
    document.getElementById('gpNombre').innerText = nombre;
    document.getElementById('gpAlias').innerText = alias;
    document.getElementById('gpDocumento').innerText = cedula;
    document.getElementById('gpDiasMora').innerText = diasMoraGlobalTemporal;
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
    if (input.files && input.files) {
        const reader = new FileReader();
        reader.onload = function(e) {
            document.getElementById(iconId).style.display = 'none';
            const img = document.getElementById(imgId);
            img.src = e.target.result;
            img.style.display = 'block';
        };
        reader.readAsDataURL(input.files);
    }
}

function confirmarAbono() {
    const clienteId = document.getElementById('abonoClienteId').value;
    const numCuota = document.getElementById('abonoNumCuota').value;
    const monto = parseFloat(document.getElementById('abonoMontoInput').value);
    if (monto > 0) {
        cerrarGestorPantallas();
        procesarPagoAPI(clienteId, numCuota, monto);
    } else {
        alert("Ingresa un monto válido mayor a $0.00");
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
    fetch('/api/marcar_pago/' + clienteId + '/' + numCuota + '?monto=' + monto + '&hora_local=' + encodeURIComponent(horaLocalFormateada) + '&zona_usuario=' + encodeURIComponent(zonaUsuario) + '&dias_mora=' + diasMoraGlobalTemporal)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                document.getElementById('modalMsg').innerText = 'Recaudo guardado para ' + data.recibo.cliente + '.';
                document.getElementById('tFecha').innerText = new Date().toLocaleDateString();
                document.getElementById('tCliente').innerText = data.recibo.cliente;
                document.getElementById('tCuota').innerText = "Procesada";
                document.getElementById('tMonto').innerText = data.recibo.monto.toFixed(2);
                document.getElementById('tSaldo').innerText = data.recibo.saldo.toFixed(2);

                const numPuro = data.recibo.telefono.toString().replace(/[^0-9]/g, '').trim();
                const textoMensaje = encodeURIComponent(data.recibo.mensaje_ws);
                
                const urlCelular = 'whatsapp://send?phone=' + numPuro + '&text=' + textoMensaje;
                const urlWeb = 'https://wa.me' + numPuro + '?text=' + textoMensaje;
                
                const btnWs = document.getElementById('modalWsBtn');
                
                // FIJADO: Cambiado '#' por javascript:void(0) para matar el parpadeo de pantalla
                btnWs.href = 'javascript:void(0)';
                
                btnWs.onclick = function(e) {
                    e.preventDefault();
                    const dropper = document.createElement('a');
                    dropper.target = '_blank';
                    dropper.rel = 'noopener noreferrer';
                    dropper.href = urlCelular;
                    document.body.appendChild(dropper);
                    dropper.click();
                    
                    setTimeout(function() {
                        dropper.href = urlWeb;
                        dropper.click();
                        document.body.removeChild(dropper);
                    }, 300);
                };
                
                document.getElementById('modalWs').style.display = 'flex';
            }
        });
}

function ejecutarNoPago(clienteId) {
    document.getElementById('gnpClienteId').value = clienteId;
    const card = document.getElementById('cliente-card-' + clienteId);
    const nombre = card ? (card.querySelector('.nombre-real')?.innerText || 'Cliente') : 'Cliente #' + clienteId;
    const alias = card ? (card.querySelector('.id-alias')?.innerText || '-') : '-';
    const cedula = card ? (card.getAttribute('data-cedula') || 'N/A') : 'N/A';
    const diasMora = card ? (card.querySelector('.circulo-check-cuota')?.innerText || '0') : '0';
    const saldoTxt = card ? (card.querySelector('.badge-saldo-premium b')?.innerText || '$0.00') : '$0.00';

    document.getElementById('gnpIdCliente').innerText = clienteId;
    document.getElementById('gnpNombre').innerText = nombre;
    document.getElementById('gnpAlias').innerText = alias;
    document.getElementById('gnpDocumento').innerText = cedula;
    document.getElementById('gnpDiasMora').innerText = diasMora;
    document.getElementById('gnpUltimoPago').innerText = card ? (card.getAttribute('data-ultpago') || 'Sin pagos previos') : '-';
    document.getElementById('gnpSaldoTexto').innerText = saldoTxt;
    document.getElementById('gnpComentarios').value = '';

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

function confirmarNoPagoGestor() {
    const clienteId = document.getElementById('gnpClienteId').value;
    cerrarGestorPantallas();
    fetch('/api/marcar_no_pago/' + clienteId)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                window.location.reload();
            } else {
                alert("Error al registrar no pago: " + (data.message || ""));
            }
        }).catch(err => alert("Error al procesar el salto: " + err));
}

function obtenerCoordenadasGPS() {
    const statusText = document.getElementById('status_gps');
    
    if (!navigator.geolocation) {
        alert("❌ Tu dispositivo no soporta geolocalización o el navegador la tiene bloqueada.");
        return;
    }
    
    statusText.innerText = "🛰️ Buscando señal de satélite GPS...";
    
    navigator.geolocation.getCurrentPosition(
        function (posicion) {
            const lat = posicion.coords.latitude;
            const lon = posicion.coords.longitude;
            
            // Asignamos las coordenadas a los inputs para guardarlas en Postgres
            document.getElementById('latitud_input').value = lat;
            document.getElementById('longitud_input').value = lon;
            
            statusText.innerHTML = `✅ Ubicación fijada: <b>${lat.toFixed(5)}, ${lon.toFixed(5)}</b>`;
            
        // CÓDIGO CORREGIDO:
        // Detecta si es un dispositivo móvil para usar el intent nativo, si no, usa el enlace web
        const urlMaps = esMovil ? `geo:${lat},${lon}?q=${lat},${lon}(Ubicación+Cliente)` : `https://google.com/maps?q=${lat},${lon}`;
        window.location.href = urlMaps;
        },
        function (error) {
            statusText.innerText = "❌ No se pudo capturar el GPS.";
            if (error.code === error.PERMISSION_DENIED) {
                alert("🚫 Permiso denegado. Debes activar el GPS y dar permisos de ubicación a tu navegador.");
            } else {
                alert("🛰️ Señal débil. Asegúrate de estar en un lugar con cobertura satelital.");
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
            console.log("⚡ Foto comprimida con éxito.");
        };
        img.src = e.target.result;
    };
    reader.readAsDataURL(file);
}

function procesarYEnviarGasto(event) {
    const fileInput = document.getElementById('foto_mov');
    const form = document.getElementById('formBalance');
    
    if (fileInput.files.length > 0 && !fotoListaParaEnviar) {
        alert("⏳ Procesando imagen de alta definición... Espera un segundo y vuelve a presionar Guardar.");
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
                    alert("⏳ La interfaz se está acomodando, por favor intenta presionar 'Ver' nuevamente.");
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
                alert("⚠️ No se pudo cargar la imagen o el registro no tiene comprobante.");
            }
        }).catch(err => alert("Error al conectar con el servidor: " + err));
}

function revertirGestionDia(tipo, id) {
    if (confirm('¿Está seguro de que desea deshacer esta gestión y regresar al cliente a la Ruta del Día?')) {
        fetch('/api/revertir_gestion/' + tipo + '/' + id)
            .then(res => res.json())
            .then(data => {
                if (data.status === 'ok') {
                    alert("✅ Gestión revertida. El cliente ha regresado a la lista por cobrar.");
                    window.location.reload();
                }
            }).catch(err => alert("Error al conectar: " + err));
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
    
    const params = new URLSearchParams({
        nombre: nom,
        cedula: ced,
        celular: cel,
        correo: cor,
        usuario: usr,
        clave: clv
    });

    fetch('/api/perfil/guardar?' + params.toString())
        .then(res => res.json())
        .then(data => {
            if(data.status === 'ok') {
                alert('👤 Datos del cobrador actualizados con éxito en la ruta.');
                window.location.reload();
            } else {
                alert('⚠️ Error al guardar los cambios.');
            }
        })
        .catch(err => console.error('Error:', err));
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

function enviarCreditoAsincrono(e) {
    e.preventDefault();
    const form = document.getElementById('formNuevoCredito');
    const formData = new FormData(form);
    
    // Deshabilitamos el botón para el control visual de carga
    const btn = document.getElementById('btnGuardarCredito');
    if(btn) { btn.disabled = true; btn.innerText = '⌛ Guardando...'; }

    fetch(form.action, {
        method: 'POST',
        body: formData,
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
    .then(res => res.json())
    .then(data => {
        if(data.status === 'ok') {
            alert('🎉 ¡Crédito e inicialización de tablas creados con éxito!');
            // Te regresa automáticamente al Dashboard principal de cobros
            window.location.href = '/';
        } else {
            alert('⚠️ Error: ' + (data.message || 'No se pudo procesar.'));
            if(btn) { btn.disabled = false; btn.innerText = 'Guardar e Inicializar Tablas'; }
        }
    })
    .catch(err => {
        console.error('Error:', err);
        alert('❌ Falla de comunicación con Neon Cloud.');
        if(btn) { btn.disabled = false; btn.innerText = 'Guardar e Inicializar Tablas'; }
    });
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
        alert('⚠️ Este cliente no tiene coordenadas GPS.');
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
        btn.innerText = '✅ Guardar';
        btn.style.background = '#10b981';
        btn.style.color = 'white';
        modoEdicionActivo = true;
    } else {
        const payload = {
            nombre: document.getElementById('perf_nombre').value,
            telefono: document.getElementById('perf_telefono').value,
            direccion: document.getElementById('perf_direccion').value,
            referencia: document.getElementById('perf_referencia').value,
            fiador: document.getElementById('perf_fiador').value
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
                alert('✅ Datos actualizados correctamente.');
                navegarRuta('/menu/cliente/' + clienteId);
            } else {
                alert('⚠️ Error al guardar: ' + res.message);
            }
        });
    }
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
</script>
</body>
</html>
"""
@app.route("/login", methods=["GET", "POST"])
def login_route():
    if request.method == "POST":
        usuario_ingresado = request.form.get("usuario")
        clave_ingresada = request.form.get("clave")
        
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT valor FROM configuracion WHERE llave = 'usuario'")
                usr_row = cursor.fetchone()
                cursor.execute("SELECT valor FROM configuracion WHERE llave = 'clave'")
                clv_row = cursor.fetchone()
        finally:
            pass
                
        # 🔐 FIJADO: Verificación utilizando hashes seguros criptográficos contra DictCursor
        if usr_row and clv_row:
            if usuario_ingresado == usr_row["valor"] and check_password_hash(clv_row["valor"], clave_ingresada):
                session["autenticado"] = True
                session["usuario"] = usuario_ingresado
                return redirect("/")
    
        return render_template_string(LOGIN_HTML, error="Credenciales incorrectas.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
def dashboard_principal():
    if not session.get("autenticado"):
        return redirect("/login")
        
    # 🔐 VALIDACIÓN INTEGRAL DE JORNADA CONTROLADA POR EL DUEÑO
    conn_val = get_db()
    try:
        with conn_val.cursor() as cur_val:
            cur_val.execute("SELECT valor FROM configuracion WHERE llave = 'caja_autorizada';")
            res_status = cur_val.fetchone()
            
            # Extraemos el valor del registro sin importar si Neon responde tupla o dict
            val_caja = "0"
            if res_status:
                if isinstance(res_status, dict):
                    val_caja = res_status.get("valor", "0")
                else:
                    val_caja = res_status[0]
            
            # Si el dueño no ha presionado el botón verde, se congela la pantalla
            if val_caja == "0" or val_caja == 0:
                return """
                <div style="text-align:center; padding:50px; font-family:sans-serif; background:#0f172a; color:white; min-height:100vh; display:flex; flex-direction:column; justify-content:center; align-items:center;">
                    <h1 style="color:#ef4444; font-size:32px; margin-bottom:10px;">⚠️ Jornada No Autorizada</h1>
                    <p style="color:#cbd5e1; font-size:16px; max-width:450px; line-height:1.6;">Tu caja se encuentra actualmente cerrada. Solicita al Administrador el pase de apertura para poder iniciar las actividades de hoy.</p>
                    <br>
                    <a href="/login" style="color:#a855f7; text-decoration:none; font-weight:bold; background:rgba(168,85,247,0.1); padding:10px 20px; border-radius:8px; border:1px solid #a855f7;">Volver al Login</a>
                </div>
                """, 403
    except Exception as e:
        print(f"Error en validación de jornada: {e}")
    finally:
        pass

    # 🛡️ CONTROL MAESTRO: Lee la jornada activa real congelada desde Neon para que nada cambie a medianoche
    cursor_j = conn_val.cursor()
    cursor_j.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
    row_j = cursor_j.fetchone()
    cursor_j.close()
    if row_j and (row_j["valor"] if isinstance(row_j, dict) else row_j[0]):
        hoy_str = row_j["valor"] if isinstance(row_j, dict) else row_j[0]
    else:
        hoy_str = date.today().isoformat()
    
    # 📅 FECHA EN TIEMPO REAL: Formateo en español para el badge superior
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    ahora = datetime.now()
    fecha_legible = f"{dias_semana[ahora.weekday()]}, {ahora.day:02d} de {meses[ahora.month - 1]} de {ahora.year}"
    
    try:
        with conn_val.cursor() as cursor:
            # 💵 1. Suma real en dólares del recaudo asentado el día de hoy
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Entrada' AND categoria = 'Cobranza' AND fecha = %s", (hoy_str,))
            recaudo_hoy = Decimal(str(cursor.fetchone()["total"]))
            
            # 📌 2. Conteo absoluto de todos los créditos entregados hoy
            cursor.execute(
                """
                SELECT COUNT(*) AS total_creditos 
                FROM balance_movimientos 
                WHERE tipo = 'Salida' AND concepto LIKE '%%CRÉDITO NUEVO%%' AND fecha = %s
                """, (hoy_str,)
            )
            res_creditos = cursor.fetchone()
            creditos_hoy = res_creditos["total_creditos"] if isinstance(res_creditos, dict) else res_creditos[0]

            # 3. Suma real del dinero prestado leyendo el flujo inmutable de Neon
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Salida' AND concepto LIKE '%%CRÉDITO NUEVO%%' AND fecha = %s", (hoy_str,))
            capital_prestado_hoy = Decimal(str(cursor.fetchone()["total"]))

            # 🎯 FIJADO DIRECTO CON TUS COLUMNAS REALES (Incluye Pendientes, Pagos Hoy y Saltados Hoy)
            cursor.execute("""
                SELECT id, nombre, telefono, direccion, referencia, identificacion, frecuencia, cuotas, valor_cuota, monto_total, orden, estado, fecha_gestion
                FROM clientes 
                WHERE estado = 'Activo' 
                  AND (frecuencia = 'Diaria' OR (frecuencia = 'Semanal' AND EXTRACT(DOW FROM CAST(%s AS DATE)) = 1))
                ORDER BY orden ASC, id DESC
            """, (hoy_str,))
            clientes_crudos = cursor.fetchall()

            # Consultamos los pagos realizados hoy por cliente para saber cuánto abonaron y sus IDs de pago
            cursor.execute("""
                SELECT id, cliente_id, valor_pagado 
                FROM pagos 
                WHERE fecha_pago_real = %s AND pagado = 1
            """, (hoy_str,))
            pagos_hoy_rows = cursor.fetchall()
            
            mapa_pagos_hoy = defaultdict(lambda: {"monto": Decimal("0.00"), "ids": []})
            for ph in pagos_hoy_rows:
                cid_h = ph["cliente_id"] if isinstance(ph, dict) else ph[1]
                pid_h = ph["id"] if isinstance(ph, dict) else ph[0]
                val_h = Decimal(str(ph["valor_pagado"] if isinstance(ph, dict) else ph[2]))
                mapa_pagos_hoy[cid_h]["monto"] += val_h
                mapa_pagos_hoy[cid_h]["ids"].append(str(pid_h))

            # Reconstruimos la estructura exacta dentro del mismo bloque del cursor
            clientes_ruta = []
            cnt_pendientes = 0
            cnt_pagos = 0
            cnt_no_pagos = 0
            cnt_diario = 0
            cnt_semanal = 0

            if clientes_crudos:
                c_ids = [c["id"] for c in clientes_crudos]
                cursor.execute("SELECT id, cliente_id, numero, valor, valor_pagado, pagado, fecha FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (c_ids,))
                todos_pagos = cursor.fetchall()
                
                pagos_mep = defaultdict(list)
                for pg in todos_pagos:
                    pagos_mep[pg["cliente_id"]].append(dict(pg))
                    
                for c in clientes_crudos:
                    d_cl = dict(c)
                    d_cl["pagos"] = pagos_mep.get(c["id"], [])
                    
                    # Determinar estado de gestión del día: 'pago', 'no_pago' o 'pendiente'
                    info_hoy = mapa_pagos_hoy.get(c["id"])
                    f_gest = str(c["fecha_gestion"]) if c.get("fecha_gestion") else ""
                    
                    if info_hoy and info_hoy["monto"] > 0:
                        d_cl["estado_hoy"] = "pago"
                        d_cl["monto_pagado_hoy"] = info_hoy["monto"]
                        d_cl["pago_ids_hoy"] = ",".join(info_hoy["ids"])
                        cnt_pagos += 1
                    elif f_gest == hoy_str:
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

                    # 📊 CÁLCULO EXACTO POR CUOTAS HÁBILES (SIN CONTAR DOMINGOS)
                    hoy_dt = date.fromisoformat(hoy_str)
                    dias_mora = 0
                    dias_adelanto = 0
                    for p in d_cl["pagos"]:
                        f_q = datetime.strptime(p["fecha"], "%Y-%m-%d").date() if isinstance(p["fecha"], str) else p["fecha"]
                        if int(p["pagado"]) == 0 and f_q < hoy_dt:
                            dias_mora += 1
                        elif int(p["pagado"]) == 1 and f_q > hoy_dt:
                            dias_adelanto += 1

                    if dias_mora > 0:
                        dias_adelanto = 0

                    d_cl["dias_mora"] = dias_mora
                    d_cl["dias_adelanto"] = dias_adelanto
                    clientes_ruta.append(d_cl)

    except Exception as e:
        print(f"Error contable en Dashboard Principal: {e}")
        recaudo_hoy = Decimal("0.00")
        creditos_hoy = 0
        capital_prestado_hoy = Decimal("0.00")
        clientes_ruta = []
        cnt_pendientes = cnt_pagos = cnt_no_pagos = cnt_diario = cnt_semanal = 0

    debido_minimo_dia = sum(Decimal(str(cl["valor_cuota"])) for cl in clientes_ruta if cl.get("estado_hoy") == "pendiente")
    
    debido_total_acumulado = Decimal("0.00")
    for cl in clientes_ruta:
        if cl.get("estado_hoy") == "pendiente":
            for p in cl["pagos"]:
                if not p["pagado"] and p["fecha"] <= hoy_str:
                    debido_total_acumulado += (Decimal(str(p["valor"])) - Decimal(str(p["valor_pagado"])))
                
    capital_en_calle = Decimal("0.00")
    for c in clientes_ruta:
        pagado_cliente = sum(Decimal(str(p["valor_pagado"])) for p in c["pagos"])
        pendiente = Decimal(str(c["monto_total"])) - pagado_cliente
        if pendiente > 0:
            capital_en_calle += pendiente

    contexto_dash = f"""
    <div class="date-badge"><i class="fa-solid fa-calendar-day"></i> {fecha_legible}</div>
    <div class="grid-kpis">
        <div class="kpi-card">
            <div class="kpi-title">Recaudo Hoy / Créditos Hoy</div>
            <div class="kpi-val">${recaudo_hoy:.2f} / {creditos_hoy} Créd.</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-title">Capital Total en Calle</div>
            <div class="kpi-val" style="color:#0f2b5c;">${capital_en_calle:.2f}</div>
        </div>
    </div>
    <div class="expected-card">
        <div class="kpi-title" style="color:#0369a1;">Recaudo Esperado del Día</div>
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">🌊 Mínimo de Ruta: <b>${debido_minimo_dia:.2f}</b> | Acumulado Mora: <b>${debido_total_acumulado:.2f}</b></div>
    </div>
    
    <!-- Barra de Búsqueda + Botón de Filtros -->
    <div style="display:flex; gap:8px; align-items:center; margin-bottom:12px;">
        <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar por nombre de cliente..." onkeyup="aplicarFiltrosDashboard()" style="margin-bottom:0; flex:1;">
        <button type="button" onclick="document.getElementById('modalFiltrosRuta').style.display='flex'" 
                style="width:44px; height:44px; border-radius:12px; background:#ffffff; border:1.5px solid #cbd5e1; color:#0f2b5c; display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; box-shadow:0 2px 5px rgba(0,0,0,0.04); -webkit-tap-highlight-color:transparent;">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#0f2b5c" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                <line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2" fill="#ffffff"/>
                <line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2" fill="#ffffff"/>
                <line x1="4" y1="18" x2="20" y2="18"/><circle cx="10" cy="18" r="2" fill="#ffffff"/>
            </svg>
        </button>
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

    <div id="clientesContainer" style="display:flex; flex-direction:column; gap:10px; width:100%; box-sizing:border-box;">
    """
    
        # 📱 PROCESAMIENTO CONTABLE PREMIUM BLINDADO Y UNIFICADO (DISEÑO LIMPIO EN DASHBOARD)
    for c in clientes_ruta:
        pagado_acumulado = sum(Decimal(str(p["valor_pagado"])) for p in c["pagos"])
        saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - pagado_acumulado)
    
        valor_cuota_dec = Decimal(str(c["valor_cuota"]))
        
        # Contador con flotantes (.1f) idéntico a TryController
        cuotas_totales_float = float(c["cuotas"])
        cuotas_pendientes_float = float(len([p for p in c["pagos"] if int(p["pagado"]) == 0]))
        cuotas_pagadas = int(c["cuotas"] - cuotas_pendientes_float)
    
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
                <button type="button" class="btn-accion-premium cobrar" title="Deshacer Cobro" 
                        onclick="if(confirm('¿Deseas revertir el cobro de ${float(monto_pag_hoy):.2f} de {c_nombre}?')){{ fetch('/api/revertir_gestion/pago/{pago_ids_hoy}').then(r=>r.json()).then(d=>{{ if(d.status==='ok') navegarRuta('/'); }}); }}"
                        style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; outline:none; -webkit-tap-highlight-color:transparent;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#16a34a" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#16a34a" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M21 26.5L24 29.5L30.5 23" stroke="#16a34a" stroke-width="2.5"/>
                    </svg>
                </button>
                <button type="button" disabled style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; padding:0; opacity:0.28; cursor:default;">
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
                <button type="button" disabled style="width:52px; height:52px; background:transparent; border:none; display:flex; align-items:center; justify-content:center; padding:0; opacity:0.28; cursor:default;">
                    <svg width="48" height="48" viewBox="0 0 36 36" fill="none" stroke="#64748b" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 7H31V20H19"/>
                        <text x="23.5" y="17.5" fill="#64748b" stroke="none" font-family="Arial, sans-serif" font-weight="900" font-size="11.5">$</text>
                        <path d="M14 9L9 14C8 15 7.5 16.5 7.5 18L9 29H16L19 20V13C19 11.5 18 10.5 16.5 10.5C15.2 10.5 14.3 11.5 14.3 13V17.5C14.3 18.8 13.3 19.5 12.2 19.5C11.1 19.5 10.3 18.8 10.3 17.5V12.5"/>
                        <path d="M21 26.5L24 29.5L30.5 23" stroke="#64748b" stroke-width="2.5"/>
                    </svg>
                </button>
                <button type="button" class="btn-accion-premium saltar" title="Deshacer No Pago" 
                        onclick="if(confirm('¿Deseas deshacer el No Pago de {c_nombre} y regresarlo a pendientes?')){{ fetch('/api/revertir_gestion/salto/{c_id}').then(r=>r.json()).then(d=>{{ if(d.status==='ok') navegarRuta('/'); }}); }}"
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

        # Inyección limpia en las tarjetas premium
        contexto_dash += f"""
        <div class="cliente-card-premium" id="cliente-card-{c_id}" data-cedula="{c.get('identificacion') or 'N/A'}" data-nombre="{c_nombre_lower}" data-mora="{clase_mora_badge}" data-estado="{estado_hoy}" data-frecuencia="{freq_slug}" style="{borde_izq_card} {display_card}">
            <div class="card-izquierda">
                {html_indicador_mora}
                <div class="info-bloque-texto" style="width:100%;">
                    <div class="id-alias">{c_id} {c_referencia}</div>
                    <div class="nombre-real">{c_nombre}</div>
                    
                    <div class="grid-metricas-premium">
                        <div class="metric-col"><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${c_valor_cuota}</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{cuotas_pendientes_float:.1f} / {cuotas_totales_float:.1f}</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pago</span>{html_col_pago}</div>
                    </div>
                    
                    <div class="fila-utilidades-premium">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}, {dias_calculados}); activarCamaraModal();">📸</button>
                        <div class="circulo-check-cuota" style="{estilo_cuadro_dias}">{texto_cuadro_dias}</div>
                        <div class="badge-saldo-premium">Saldo <b>${c_saldo_restante}</b></div>
                    </div>
                </div>
            </div>    
            <div class="card-derecha-acciones" id="acciones-{c_id}">
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
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT MAX(id) AS id, MAX(TRIM(nombre)) AS nombre, MAX(telefono) AS telefono, MAX(referencia) AS referencia, MAX(identificacion) AS cedula
                FROM clientes 
                GROUP BY LOWER(TRIM(nombre))
                ORDER BY MAX(TRIM(nombre)) ASC
            """)
            lista_unica = [dict(c) for c in cursor.fetchall()]
    except Exception as e:
        print(f"Error al consultar clientes: {e}")
        lista_unica = []
        
    contexto = dict(vista="clientes_maestro", lista_clientes=lista_unica)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))


@app.route("/menu/cliente/<int:cliente_id>")
def perfil_cliente_detalle(cliente_id):
    if not session.get("autenticado"):
        return "Sesión expirada"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
            cliente = dict(cursor.fetchone())
            
            cursor.execute("""
                SELECT c.id, c.fecha_inicio, c.monto_total, c.estado,
                       COALESCE((SELECT SUM(valor_pagado) FROM pagos WHERE cliente_id = c.id), 0) AS pagado
                FROM clientes c
                WHERE c.nombre = %s
                ORDER BY c.id DESC
            """, (cliente["nombre"],))
            historial_creditos = []
            for cr in cursor.fetchall():
                cr_dict = dict(cr)
                cr_dict["saldo"] = max(Decimal("0.00"), Decimal(str(cr_dict["monto_total"])) - Decimal(str(cr_dict["pagado"])))
                historial_creditos.append(cr_dict)
    except Exception as e:
        return f"Error al cargar ficha: {e}", 500

    contexto = dict(vista="perfil_cliente", cliente=cliente, creditos=historial_creditos)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))


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
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Optimizamos la consulta principal de todos los clientes bajo el filtro seleccionado
            cursor.execute(
                "SELECT id, nombre, frecuencia, monto_total, estado, saltado_hoy, valor_cuota FROM clientes WHERE estado = %s ORDER BY nombre ASC", 
                (filtro_estado,)
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
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Activo'")
            total_activos = int(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Inactivo'")
            total_inactivos = int(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Lista Negra'")
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
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # Recuperamos la jornada activa para que el balance no se vacíe a medianoche
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
            row_j = cursor.fetchone()
            if row_j and row_j["valor"]:
                hoy_str = row_j["valor"]
            else:
                hoy_str = date.today().isoformat()
                cursor.execute(
                    "INSERT INTO configuracion (llave, valor) VALUES ('fecha_jornada_activa', %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                    (hoy_str,)
                )
                conn.commit()

            ## 1. CÁLCULO DINÁMICO DE CAJA BASE - ARRASTRE REAL DE CARTERA NEON
            cursor.execute(
                """
                SELECT 
                    COALESCE(SUM(CASE WHEN tipo = 'Entrada' THEN monto ELSE 0 END), 0) -
                    COALESCE(SUM(CASE WHEN tipo = 'Salida' THEN monto ELSE 0 END), 0)
                FROM balance_movimientos
                WHERE fecha < %s
                """,
                (hoy_str,)
            )
            res_saldo_historico = cursor.fetchone()
        
            # Extraemos el valor de forma segura sin importar si el cursor devuelve tupla o diccionario
            if res_saldo_historico:
                if isinstance(res_saldo_historico, dict):
                    valores_lista = list(res_saldo_historico.values())
                    saldo_acumulado_ayer = Decimal(str(valores_lista[0])) if valores_lista and valores_lista[0] is not None else Decimal("0.00")
                else:
                    saldo_acumulado_ayer = Decimal(str(res_saldo_historico[0])) if res_saldo_historico and res_saldo_historico[0] is not None else Decimal("0.00")
            else:
                saldo_acumulado_ayer = Decimal("0.00")

            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base'")
            res_caja = cursor.fetchone()

            if res_caja:
                if isinstance(res_caja, dict):
                    caja_inicial_sistema = Decimal(str(res_caja.get("valor", "100.00") or "100.00"))
                else:
                    caja_inicial_sistema = Decimal(str(res_caja[0] or "100.00"))
            else:
                caja_inicial_sistema = Decimal("100.00")
            
            # La caja base real de hoy es el acumulado histórico de flujos hasta el día de ayer
            caja_base = caja_inicial_sistema + saldo_acumulado_ayer
            
            # 📊 2. Consultamos si la jornada ya fue sellada hoy
            cursor.execute("SELECT valor FROM configuracion WHERE llave = %s", (f"cierre_caja_{hoy_str}",))
            res_cierre = cursor.fetchone()
            estado_cierre = res_cierre["valor"] if res_cierre else "Abierta"
            
            # 📝 3. Traemos los movimientos manuales de la tabla de flujos
            cursor.execute("SELECT id, tipo, categoria, concepto, monto, fecha, comprobante FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
            movimientos = cursor.fetchall()
            
            # 🪙 4. Sumamos en tiempo real los abonos en efectivo cobrados en la calle hoy
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Entrada' AND categoria = 'Cobranza' AND fecha = %s", (hoy_str,))
            total_abonos_hoy = Decimal(str(cursor.fetchone()["total"]))
            
            # 🛡️ NUEVO QUERY: Sumatoria de los microseguros cobrados hoy en Neon
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Entrada' AND concepto LIKE '%%MICROSEGURO%%' AND fecha = %s", (hoy_str,))
            total_seguros_hoy = Decimal(str(cursor.fetchone()["total"]))

            # 📊 5. Calculamos el dinero prestado real leyendo las transacciones de Neon
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Salida' AND concepto LIKE '%%CRÉDITO NUEVO%%' AND fecha = %s", (hoy_str,))
            total_prestado_hoy = Decimal(str(cursor.fetchone()["total"]))
    except Exception as e:
        print(f"Error en balance: {e}")
        caja_base = total_abonos_hoy = total_seguros_hoy = total_prestado_hoy = Decimal("0.00")
        movimientos = []
        estado_cierre = "Abierta"
    finally:
        pass
        
    # #️⃣ Agrupamos los flujos de la tabla de movimientos manuales con precisión Decimal
    entradas_manuales = sum(Decimal(str(m["monto"])) for m in movimientos if m["tipo"] == "Entrada" and "MICROSEGURO" not in m["concepto"].upper() and m["categoria"] != "Cobranza")
    salidas_manuales = sum(Decimal(str(m["monto"])) for m in movimientos if m["tipo"] == "Salida" and "CRÉDITO NUEVO" not in m["concepto"].upper())

    # 📊 FÓRMULA CONTABLE UNIVERSAL MOSTRANDO VALORES NETOS REALES
    entradas_totales = total_abonos_hoy + total_seguros_hoy + entradas_manuales
    salidas_totales = salidas_manuales + total_prestado_hoy
    efectivo_neto = caja_base + entradas_totales - salidas_totales
    
    # 🧼 Clean y purificación de imágenes Base64 de los comprobantes
    movimientos_limpios = []
    for m in movimientos:
        if m["categoria"] == "Cobranza":
            continue
        m_dict = dict(m)
        comprobante_puro = ""
        if m_dict.get("comprobante"):
            try:
                raw_comp = m_dict["comprobante"]
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
            except Exception:
                comprobante_puro = ""
        m_dict["comprobante"] = comprobante_puro
        m_dict["monto"] = float(m_dict["monto"])
        movimientos_limpios.append(m_dict)

    # Añadimos al desglose visual un registro informativo de lo recaudado en la calle
    if total_abonos_hoy > 0:
        movimientos_limpios.insert(0, {
            "id": 0, "tipo": "Entrada", "categoria": "Recaudo",
            "concepto": "🔸 RECAUDACIÓN DE CUOTAS EN RUTA",
            "monto": float(total_abonos_hoy), "fecha": hoy_str, "comprobante": ""
        })
        
    # 🗂️ Armando el diccionario de contexto corrigiendo todos los guiones por signos de igual (=)
    contexto = dict(
        vista="balance",
        caja_base=float(caja_base),
        balance_estado=estado_cierre,  # Enlazado al estado real de cierre
        gastos_list=movimientos_limpios,
        entradas_totales=float(entradas_totales),
        salidas_totales=float(salidas_totales),
        efectivo_neto=float(efectivo_neto)
    )
    
    # 🛡️ FIJADO: Operadores de asignación e igualdad correctos para el refresco del celular
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
    
    try:
        monto = Decimal(request.form.get("monto", "0"))
        # 📌 CORREGIDO: Se acopla al 'interes_defecto' del maestro de Neon
        interes_porcentaje = Decimal(request.form.get("interes_defecto", "20"))
        cuotas = int(request.form.get("cuotas", "1"))
    except Exception:
        return jsonify({"status": "error", "message": "Valores numéricos inválidos"}), 400

    frecuencia = request.form.get("frecuencia", "Diaria")
    # 📌 CORREGIDO: Se empareja con el name real de tus opciones HTML
    aplica_seguro = request.form.get("aplicar_seguro", "NO")
    
    latitud = request.form.get("latitud", "")
    longitud = request.form.get("longitud", "")
    
    fecha_inicio_str = request.form.get("fecha_inicio", date.today().isoformat())
    try:
        fecha_inicio_dt = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
    except ValueError:
        fecha_inicio_dt = date.today()

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
                    
            # 🛡️ 1. Inserción limpia del microseguro (Fuerza la fecha de HOY real)
            if microseguro_valor > 0:
                concepto_seguro = f"🛡️ RECAUDO MICROSEGURO - CLIENTE: {nombre}"
                cursor.execute(
                    """
                    INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante)
                    VALUES ('Entrada', 'Inversión', %s, %s, %s, '')
                    """,
                    (concepto_seguro, microseguro_valor, date.today().isoformat())
                )
            
            # 💸 2. ÚNICO ASIENTO DE EGRESO DE CAPITAL (Fuerza la fecha de HOY real)
            concepto_egreso = f"📉 CAPITAL ENTREGADO EN CRÉDITO NUEVO - CLIENTE: {nombre}"
            cursor.execute(
                """
                INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante)
                VALUES ('Salida', 'Inversión', %s, %s, %s, '')
                """,
                (concepto_egreso, monto, date.today().isoformat())
            )
            
            # 📊 3. Calcular posición secuencial de enrutamiento (Blindado contra DictRow)
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes")
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

            # Insertar cabecera con 17 parámetros alineados milimétricamente
            cursor.execute(
                """
                INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, latitud, longitud, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo') RETURNING id
                """,
                (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat(), latitud, longitud)
            )
            res_id = cursor.fetchone()
        
            # 👤 LÍNEA 2113 COMPLETAMENTE BLINDADA:
            if res_id is None:
                cliente_id = None
            elif isinstance(res_id, dict):
                cliente_id = res_id.get("id")
            elif hasattr(res_id, '__getitem__'):
                cliente_id = res_id[0]
            else:
                cliente_id = res_id
            
            # Tablas de amortización protegidas contra desfases de bucle semanales
            if cliente_id:
                acumulado = Decimal("0.00")
                for num in range(1, cuotas + 1):
                    if num == cuotas:
                        valor_cuota_real = monto_total - acumulado
                    else:
                        valor_cuota_real = valor_cuota
                        
                    acumulado += valor_cuota_real
                    
                    try:
                        f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frequency=frecuencia)
                    except Exception:
                        from datetime import timedelta
                        f_cuota = fecha_inicio_dt + timedelta(days=(num - 1) * 7 if frecuencia == "Semanal" else (num - 1))
                        
                    cursor.execute(
                        """
                        INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real)
                        VALUES (%s, %s, %s, %s, 0, 0, '')
                        """,
                        (cliente_id, num, f_cuota.isoformat(), valor_cuota_real)
                    )
                    
        conn.commit()
        return jsonify({"status": "ok", "message": "Crédito guardado exitosamente"})
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
    concepto = request.form.get("concepto_mov", "").strip()
    hoy_str = date.today().isoformat()
    
    # 💰 Captura de dinero con precisión contable exacta Decimal
    monto = Decimal(request.form.get("monto_mov", "0"))
    
    if not concepto: 
        concepto = f"Flujo de {categoria}"
    
    # 📸 Captura de la cadena de imagen comprimida en caliente desde el móvil
    base64_str = request.form.get("foto_comprimida_b64", "").strip()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante) 
                VALUES (%s, %s, %s, %s, %s, %s)
                """, (tipo, categoria, concepto, monto, hoy_str, base64_str)
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al guardar movimiento de balance: {e}")
    finally:
        pass
        
    return redirect("/menu/balance")

@app.route("/api/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Estructura segura con liberación obligatoria del canal en el pool
            cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (gasto_id,))
        conn.commit()
    except Exception as e:
        print(f"Error al eliminar gasto contable: {e}")
    finally:
        pass
        
    return redirect("/menu/balance")

@app.route("/api/eliminar_cliente/<int:id>")
def api_eliminar_cliente(id):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 1. Primero obtenemos el nombre del cliente antes de borrarlo para poder limpiar sus flujos
            cursor.execute("SELECT nombre FROM clientes WHERE id = %s;", (id,))
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
            cursor.execute("DELETE FROM pagos WHERE cliente_id = %s;", (id,))
            
            # 👤 4. Eliminamos definitivamente la ficha del cliente
            cursor.execute("DELETE FROM clientes WHERE id = %s;", (id,))
            
        conn.commit()
    except Exception as e:
        print(f"Error crítico al eliminar cliente de la base de datos: {e}")
    finally:
        pass
        
    return redirect("/menu/clientes")

@app.route("/mover/<int:cliente_id>/<string:direccion>")
def mover(cliente_id, direccion):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Recuperamos todos los clientes activos ordenados para calcular posiciones en memoria
            cursor.execute("SELECT id, orden FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            
            # 🛡️ FIJADO: Operador de comparación exacto para hallar la posición del cliente
            index = next((i for i, c in enumerate(clientes) if c["id"] == cliente_id), None)
            
            if index is not None:
                # 🛡️ FIJADO: Comparación limpia de dirección para SUBIR en la ruta
                if direccion == "subir" and index > 0:
                    actual = clientes[index]
                    anterior = clientes[index - 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (anterior["orden"], actual["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (actual["orden"], anterior["id"]))
                    
                # 🛡️ FIJADO: Comparación limpia de dirección para BAJAR en la ruta
                elif direccion == "bajar" and index < len(clientes) - 1:
                    actual = clientes[index]
                    siguiente = clientes[index + 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (siguiente["orden"], actual["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (actual["orden"], siguiente["id"]))
                    
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
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Traemos los clientes activos en su orden actual de secuencia
            cursor.execute("SELECT id, nombre, orden, referencia, frecuencia FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            todos = cursor.fetchall()
    except Exception as e:
        print(f"Error al cargar orden de ruta: {e}")
        todos = []
    finally:
        pass

    # 🎨 FIJADO: Enlace oficial a FontAwesome para activar los iconos de las flechas en el móvil
    html_orden = """
    <link rel="stylesheet" href="https://cloudflare.com">
    
    <div class="section-header-title" style="margin-bottom: 15px; font-weight: 800; color: #0f2b5c;">
        <i class="fa-solid fa-arrow-down-up-lock"></i> Organizar Secuencia de Ruta
    </div>
    <p style="font-size:11px; color:#64748b; margin-bottom:16px; text-align:left; padding:0 4px; line-height: 1.4;">
        Ajusta el orden de visita diario. Presiona las flechas de forma consecutiva para acomodar la posición de tus cobradores al instante.
    </p>
    <div style="display: flex; flex-direction: column; gap: 10px;">
    """

    for index, cl in enumerate(todos):
        html_orden += f"""
        <div style="background:#ffffff; padding:12px; display:flex; justify-content:space-between; align-items:center; border-radius:12px; border-left:4px solid #0284c7; box-shadow:0 2px 5px rgba(0,0,0,0.04);">
            <div style="text-align:left; display:flex; align-items:center; gap:10px; flex: 1; min-width: 0;">
                <!-- 🎯 Círculo con el número de posición actual -->
                <div style="background:#0f2b5c; color:white; font-size:11px; font-weight:800; border-radius:50%; width:26px; height:26px; display:flex; align-items:center; justify-content:center; flex-shrink: 0;">
                    {index + 1}
                </div>
                <div style="min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                    <span style="font-weight:800; font-size:13px; color:#0f2b5c; display:block; overflow: hidden; text-overflow: ellipsis;">{cl['nombre']}</span>
                    <span style="font-size:10px; color:#64748b; display:block; margin-top:2px; overflow: hidden; text-overflow: ellipsis;">📍 Ref: {cl['referencia'] or 'Comercio'}</span>
                </div>
            </div>
            
            <!-- ⚡ BOTONES INTERACTIVOS CIRCULARES CON SVG CENTRADOS -->
            <div style="display:flex; gap:8px; flex-shrink: 0; margin-left: 10px; align-items: center;">
                <!-- Botón Subir -->
                <a href="/mover/{cl['id']}/subir" style="text-decoration:none; width:36px; height:36px; background:#f1f5f9; color:#475569; border-radius:50%; display:flex; align-items:center; justify-content:center; transition: background 0.2s;" title="Subir">
                    <svg xmlns="http://w3.org" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="18 15 12 9 6 15"></polyline>
                    </svg>
                </a>
                <!-- Botón Bajar -->
                <a href="/mover/{cl['id']}/bajar" style="text-decoration:none; width:36px; height:36px; background:#f1f5f9; color:#475569; border-radius:50%; display:flex; align-items:center; justify-content:center; transition: background 0.2s;" title="Bajar">
                    <svg xmlns="http://w3.org" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="6 9 12 15 18 9"></polyline>
                    </svg>
                </a>
            </div>
        </div>
        """
        
    if not todos:
        html_orden += '<p style="font-size:12px; color:#64748b; text-align:center; padding:30px;">👥 No hay clientes activos para ordenar en la ruta hoy.</p>'
        
    html_orden += "</div>"

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_orden)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_orden))

@app.route("/menu/listados")
def seccion_listados():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    fecha_filtro = request.args.get("fecha_auditoria", date.today().isoformat())
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Consultamos los pagos individuales con precisión contable
            cursor.execute(
                """
                SELECT p.numero, p.valor_pagado, c.id AS cliente_id, c.nombre 
                FROM pagos p 
                JOIN clientes c ON p.cliente_id = c.id 
                WHERE p.fecha_pago_real = %s AND p.pagado = 1
                ORDER BY c.nombre ASC, p.numero ASC
                """, (fecha_filtro,)
            )
            pagos_f = cursor.fetchall()
            
            # Consultamos los créditos entregados en la fecha
            cursor.execute("SELECT id, nombre, monto, monto_total FROM clientes WHERE fecha_inicio = %s", (fecha_filtro,))
            creditos_f = cursor.fetchall()
    except Exception as e:
        print(f"Error al consultar auditoría histórica: {e}")
        pagos_f = []
        creditos_f = []
    finally:
        pass
            
    # 🗂️ AGRUPACIÓN PARA EVITAR NOMBRES REPETIDOS
    mapa_pagos = {}
    for p in pagos_f:
        c_id = p["cliente_id"]
        v_pagado = Decimal(str(p["valor_pagado"]))
        
        if c_id not in mapa_pagos:
            mapa_pagos[c_id] = {
                "nombre": p["nombre"],
                "cuotas": [p["numero"]],
                "total_abonado": v_pagado
            }
        else:
            mapa_pagos[c_id]["cuotas"].append(p["numero"])
            mapa_pagos[c_id]["total_abonado"] += v_pagado

    # 🎨 Construimos el diseño HTML limpio y consolidado
    lista_pagos_html = []
    for c_id, info in mapa_pagos.items():
        min_cuota = min(info["cuotas"])
        max_cuota = max(info["cuotas"])
        
        if min_cuota == max_cuota:
            rango = f"Cuota #{min_cuota}"
        else:
            rango = f"Cuotas #{min_cuota} a la #{max_cuota}"
            
        lista_pagos_html.append(
            f"<p style='font-size:12px; border-bottom:1px solid #f1f5f9; padding:6px 0; margin:0;'>➡️ <b>{info['nombre']}</b> — {rango} | Recaudado: <b style='color:#10b981;'>${float(info['total_abonado']):.2f}</b></p>"
        )
        
    html_pagos_final = "".join(lista_pagos_html)
    html_creditos_final = "".join([f"<p style='font-size:12px; border-bottom:1px solid #f1f5f9; padding:6px 0; margin:0;'>➡️ <b>{c['nombre']}</b> | Capital: <b>${float(c['monto']):.2f}</b> | Cartera: <b>${float(c['monto_total']):.2f}</b></p>" for c in creditos_f])
    
    if not html_pagos_final: 
        html_pagos_final = "<p style='font-size:11px; color:#64748b; padding:4px 0;'>Ningún recaudo asentado en esta fecha.</p>"
    if not html_creditos_final: 
        html_creditos_final = "<p style='font-size:11px; color:#64748b; padding:4px 0;'>Ningún crédito entregado en esta fecha.</p>"

    html_listados = f"""
    <div class="section-header-title"><i class="fa-solid fa-list-check"></i> Auditoría Histórica por Fecha</div>
    <div class="card" style="border-radius:12px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
        <form onsubmit="event.preventDefault(); const f = document.getElementById('fecha_auditoria').value; navegarRuta('/menu/listados?fecha_auditoria=' + f);">
            <label style="font-weight:700; color:#0f2b5c; font-size:12px;">Selecciona la Fecha a Consultar</label>
            <input type="date" id="fecha_auditoria" value="{fecha_filtro}" required style="margin-bottom:10px; width:100%; padding:8px; border-radius:6px; border:1px solid #cbd5e1;">
            <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:10px; font-weight:bold;">🔍 Consultar Historial</button>
        </form>
    </div>
    
    <div class="card" style="text-align:left; margin-top:12px; border-radius:12px;">
        <h4 style="font-size:12px; color:#0f2b5c; margin-bottom:8px; border-bottom:2px solid #f1f5f9; padding-bottom:4px;"><i class="fa-solid fa-money-bill-wave"></i> Recaudos de la Fecha</h4>
        {html_pagos_final}
    </div>
    
    <div class="card" style="text-align:left; margin-top:12px; border-radius:12px;">
        <h4 style="font-size:12px; color:#0f2b5c; margin-bottom:8px; border-bottom:2px solid #f1f5f9; padding-bottom:4px;"><i class="fa-solid fa-chart-line"></i> Créditos Entregados</h4>
        {html_creditos_final}
    </div>
    """
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_listados)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_listados))

@app.route("/menu/agregar_lista")
def seccion_agregar_lista():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Traemos los clientes activos ordenados alfabéticamente para el selector desplegable
            cursor.execute("SELECT id, nombre, frecuencia FROM clientes WHERE estado = 'Activo' ORDER BY nombre ASC")
            todos = cursor.fetchall()
    except Exception as e:
        print(f"Error al cargar enrutamiento forzado: {e}")
        todos = []
    finally:
        pass

    # Convertimos los registros a diccionarios puros legibles por el motor de renderizado
    todos_procesados = [dict(c) for c in todos]

    html_sincro = """
    <div class="section-header-title"><i class="fa-solid fa-user-plus"></i> Enrutamiento Forzado Anticipado</div>
    <div class="card" style="border-radius:12px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
        <form action="/api/clientes/forzar_enrutado" method="POST">
            <label style="font-weight:700; color:#0f2b5c; font-size:12px;">Selecciona al Cliente Semanal o Mensual a Forzar Cobro Hoy</label>
            <select name="cliente_forzar_id" required style="width:100%; padding:10px; border-radius:6px; border:1px solid #cbd5e1; margin-bottom:12px;">
                {% for c in todos %}
                    <option value="{{ c['id'] }}">{{ c['nombre'] }} ({{ c['frecuencia'] }})</option>
                {% endfor %}
            </select>
            <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:12px; font-weight:bold;">⚡ Forzar Aparición en Ruta</button>
        </form>
    </div>
    """

    contexto = dict(todos=todos_procesados)

    # 🪙 FIJADO: Operador de comparación exacto compatible con el refresco dinámico del móvil
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
            cursor.execute("UPDATE clientes SET enrutado_forzado = 1 WHERE id = %s", (c_id,))
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
    
    # 🔄 RESPALDO REAL: Verificación física y conteo de consistencia en Neon
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
            row_f = cursor.fetchone()
            hoy_str = row_f["valor"] if (row_f and row_f["valor"]) else date.today().isoformat()
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes")
            res_c = cursor.fetchone()
            total_clientes = res_c["total"] if isinstance(res_c, dict) else res_c[0]
            
            cursor.execute("SELECT COUNT(*) AS total FROM pagos WHERE pagado = 1")
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

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    monto_recaudado = Decimal(str(request.args.get("monto", 0)))
    hora_local_cliente = request.args.get("hora_local", "")
    if not hora_local_cliente:
        hora_local_cliente = datetime.now().strftime("%d/%m/%Y %H:%M")
            
    conn = get_db()
    with conn.cursor() as cur_f:
        cur_f.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
        row_f = cur_f.fetchone()
        hoy_str = row_f["valor"] if (row_f and row_f["valor"]) else date.today().isoformat()
        
    hoy_date = date.fromisoformat(hoy_str)
    monto_restante = monto_recaudado
    monto_efectivamente_aplicado = Decimal("0.00")
    try:
        with conn.cursor() as cursor:
            # 1. Aplicamos el dinero recibido a las cuotas pendientes cronológicamente
            cursor.execute("SELECT id, numero, fecha, valor, valor_pagado, pagado FROM pagos WHERE cliente_id = %s AND pagado = 0 ORDER BY numero ASC", (cliente_id,))
            pagos_pendientes = cursor.fetchall()
            
            for p in pagos_pendientes:
                if monto_restante <= 0:
                    break
                    
                pendiente_cuota = Decimal(str(p["valor"])) - Decimal(str(p["valor_pagado"]))
                
                if monto_restante >= pendiente_cuota:
                    monto_restante = round(monto_restante - pendiente_cuota, 2)
                    monto_efectivamente_aplicado += pendiente_cuota
                    cursor.execute(
                        "UPDATE pagos SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (p["valor"], hoy_str, p["id"])
                    )
                else:
                    nuevo_pago_parcial = round(Decimal(str(p["valor_pagado"])) + monto_restante, 2)
                    monto_efectivamente_aplicado += monto_restante
                    monto_restante = Decimal("0.00")
                    cursor.execute(
                        "UPDATE pagos SET valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (nuevo_pago_parcial, hoy_str, p["id"])
                    )

            # 2. Evaluamos el estado REAL de la cuenta DESPUÉS de aplicar el pago
            cursor.execute("SELECT fecha, pagado FROM pagos WHERE cliente_id = %s ORDER BY numero ASC", (cliente_id,))
            estado_final_cuotas = cursor.fetchall()
            
            dias_mora_real = 0
            dias_adelanto_real = 0
            tiene_mora = False

            for q in estado_final_cuotas:
                f_q = datetime.strptime(q["fecha"], "%Y-%m-%d").date() if isinstance(q["fecha"], str) else q["fecha"]
                if int(q["pagado"]) == 0 and f_q < hoy_date:
                    tiene_mora = True
                    dias_mora_real += 1
                elif int(q["pagado"]) == 1 and f_q > hoy_date:
                    dias_adelanto_real += 1

            if tiene_mora:
                dias_adelanto_real = 0

            # 3. Recalcular saldo total real del cliente
            cursor.execute("SELECT nombre, telefono, monto_total FROM clientes WHERE id = %s", (cliente_id,))
            c = cursor.fetchone()
            
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE cliente_id = %s", (cliente_id,))
            total_pagado = Decimal(str(cursor.fetchone()["total"]))
            saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - total_pagado)
            
            if saldo_restante <= 0:
                cursor.execute("UPDATE clientes SET estado = 'Inactivo', fecha_gestion = %s, enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (hoy_str, cliente_id))
            else:
                cursor.execute("UPDATE clientes SET fecha_gestion = %s, enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (hoy_str, cliente_id))

            # 4. Asentar únicamente el dinero neto cobrado hoy en el balance
            if monto_efectivamente_aplicado > 0:
                concepto_abono = f"💵 ABONO CUOTA - CLIENTE: {c['nombre']} (ID:{cliente_id})"
                cursor.execute(
                    """
                    INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante)
                    VALUES ('Entrada', 'Cobranza', %s, %s, %s, '')
                    """,
                    (concepto_abono, monto_efectivamente_aplicado, hoy_str)
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
    
    recibo = {
        "cliente": c["nombre"],
        "telefono": c["telefono"] or "",
        "cuota": num_cuota,
        "monto": float(monto_efectivamente_aplicado),
        "saldo": float(saldo_restante),
        "mensaje_ws": mensaje_ws
    }
    return jsonify({"status": "ok", "recibo": recibo})


@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    conn = get_db()
    with conn.cursor() as cur_f:
        cur_f.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
        row_f = cur_f.fetchone()
        hoy_str = row_f["valor"] if (row_f and row_f["valor"]) else date.today().isoformat()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Incrementa el contador de saltos del día y bloquea la fuga en el pool de Neon
            cursor.execute(
                "UPDATE clientes SET saltado_hoy = saltado_hoy + 1, fecha_gestion = %s WHERE id = %s",
                (hoy_str, cliente_id)
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al registrar no pago del cliente: {e}")
        return jsonify({"status": "error", "message": "Error interno del servidor"})
    finally:
        pass
        
    return jsonify({"status": "ok"})

@app.route("/menu/bluetooth")
def seccion_bluetooth_impresora():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    # 🎨 INTERFAZ PREMIUM TÁCTIL CONTROLADA POR WEB BLUETOOTH API
    html_bluetooth = """
    <link rel="stylesheet" href="https://cloudflare.com">
    
    <div class="section-header-title" style="margin-bottom: 12px; font-weight: 800; color: #0f2b5c;">
        <i class="fa-solid fa-print"></i> Conexión de Impresora Térmica
    </div>
    <p style="font-size:11px; color:#64748b; margin-bottom:14px; text-align:left; padding:0 4px; line-height:1.4;">
        Enlaza tu mini impresora térmica portátil de 58mm directamente desde el celular para emitir tickets físicos al instante durante la cobranza en ruta.
    </p>
    
    <!-- PANEL DE ESTADO INTERACTIVO -->
    <div class="card" style="text-align:center; padding:24px 16px; border-radius:12px; box-shadow:0 2px 4px rgba(0,0,0,0.02); margin-bottom:12px; background:#ffffff;">
        <i class="fa-solid fa-print" id="icon_bt_status" style="font-size:48px; color:#94a3b8; margin-bottom:12px; transition: color 0.3s;"></i>
        <h4 style="font-size:14px; color:#0f2b5c; margin:0 0 4px 0; font-weight:800;">Estado de la Impresora</h4>
        <p id="text_bt_status" style="font-size:11px; color:#64748b; margin-bottom:16px;">Desconectada — Hardware libre</p>
        
        <div style="display:flex; flex-direction:column; gap:8px;">
            <button type="button" onclick="conectarImpresoraBluetooth();" class="btn-accion btn-pagar" style="width:100%; padding:12px; font-weight:bold; font-size:12px; background:#0284c7; box-shadow:0 2px 4px rgba(2,132,199,0.2);">
                📡 Vincular Impresora (58mm)
            </button>
            <button type="button" onclick="probarImpresionTicket();" id="btn_probar_print" disabled class="btn-accion" style="width:100%; padding:12px; font-weight:bold; font-size:12px; background:#64748b; color:white; border:none; border-radius:6px;">
                📄 Imprimir Ticket de Prueba
            </button>
        </div>
    </div>

    <!-- 🧠 MOTOR DE COMUNICACIÓN DE HARDWARE EN JAVASCRIPT (WEB BLUETOOTH) -->
    <script>
    let dispositivoBluetooth = null;
    let caracteristicaGatt = null;

    async function conectarImpresoraBluetooth() {
        const iconStatus = document.getElementById('icon_bt_status');
        const textStatus = document.getElementById('text_bt_status');
        const btnProbar = document.getElementById('btn_probar_print');
        
        try {
            textStatus.innerText = "📡 Buscando hardware térmico cercano...";
            
            // Filtramos la búsqueda para capturar cualquier impresora con perfil de puerto serial de fábrica
            dispositivoBluetooth = await navigator.bluetooth.requestDevice({
                filters: [{ services: ['00001101-0000-1000-8000-00805f9b34fb'] }],
                optionalServices: ['000018f0-0000-1000-8000-00805f9b34fb'] // Servicios genéricos de impresión
            });
            
            textStatus.innerText = `⏳ Enlazando con ${dispositivoBluetooth.name}...`;
            const servidorGatt = await dispositivoBluetooth.gatt.connect();
            
            // Intentamos capturar los canales de escritura estándar de las mini impresoras de 58mm
            const servicios = await servidorGatt.getPrimaryServices();
            if(servicios.length > 0) {
                const caracteristicas = await servicios[0].getCharacteristics();
                caracteristicaGatt = caracteristicas.find(c => c.properties.write || c.properties.writeWithoutResponse);
            }
            
            if (caracteristicaGatt) {
                iconStatus.style.color = "#10b981";
                textStatus.innerHTML = `✅ Conectado con éxito a:<br><b>${dispositivoBluetooth.name}</b>`;
                btnProbar.disabled = false;
                btnProbar.style.background = "#10b981";
                alert("🎯 ¡Impresora térmica de 58mm vinculada con éxito y lista para trabajar!");
            } else {
                throw new Error("No se encontró canal de escritura compatible.");
            }
            
        } catch (error) {
            console.error(error);
            iconStatus.style.color = "#ef4444";
            textStatus.innerText = "❌ Falla de vinculación Bluetooth.";
            alert("⚠️ No se pudo concretar el enlace. Asegúrate de encender el Bluetooth del celular, prender la impresora térmica y dar permisos de emparejamiento.");
        }
    }

    async function probarImpresionTicket() {
        if (!caracteristicaGatt) return;
        
        try {
            // Comandos ESC/POS nativos para limpiar, centrar y dar saltos de línea físicos
            const encoder = new TextEncoder();
            const comandoInicio = new Uint8Array([0x1B, 0x40, 0x1B, 0x61, 0x01]); // Reset y centrado
            const textoTicket = encoder.encode("\\n🧾 AVANTA PAGOS 🧾\\n* Ticket de Prueba Exitoso *\\n--------------------------------\\nImpresora Termica 58mm\\nConectada Correctamente\\n--------------------------------\\n\\n\\n\\n");
            
            await caracteristicaGatt.writeValue(comandoInicio);
            await caracteristicaGatt.writeValue(textoTicket);
            alert("🖨️ ¡Comando enviado! Revisa si tu mini impresora expulsó el papel de prueba.");
        } catch (error) {
            alert("⚠️ Falla al transmitir datos a la impresora: " + error.message);
        }
    }
    </script>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_bluetooth)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_bluetooth))

@app.route("/menu/configuracion")
def seccion_configuracion_sistema():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Recuperamos todos los parámetros dinámicos del sistema de una sola vez
            cursor.execute("SELECT llave, valor FROM configuracion")
            filas = cursor.fetchall()
            config = {f["llave"]: f["valor"] for f in filas}
    finally:
        pass

    # Variables de control con valores de respaldo de seguridad
    usuario_actual = config.get("usuario", "admin")
    empresa_nombre = config.get("empresa_nombre", "AVANTA PAGOS")
    empresa_telefono = config.get("empresa_telefono", "593991234567")
    ticket_encabezado = config.get("ticket_encabezado", "¡BIENVENIDO A AVANTA PAGOS!")
    interes_defecto = config.get("interes_defecto", "20")
    caja_base = config.get("caja_base", "100.00")

    # 🎨 INTERFAZ CREATIVA MULTISECCIÓN PREMIUM (DISEÑO MÓVIL ESTILO IOS/ANDROID)
    html_config = f"""
    <div class="section-header-title" style="margin-bottom: 15px; font-weight: 800; color: #0f2b5c;">
        <i class="fa-solid fa-sliders"></i> Centro de Control Maestro
    </div>
    <p style="font-size:11px; color:#64748b; margin-bottom:16px; text-align:left; padding:0 4px; line-height: 1.4;">
        Calibra los parámetros de la empresa, el porcentaje de rentabilidad, los tickets de WhatsApp y la seguridad física del sistema de cobros.
    </p>

    <form action="/api/configuracion/guardar_maestro" method="POST" onsubmit="alert('💾 Configuración maestra guardada y sincronizada con Neon AWS.');" style="display: flex; flex-direction: column; gap: 14px;">
        
        <!-- 🟢 SECCIÓN 1: PARAMETRIZACIÓN DE LA EMPRESA -->
        <div class="card" style="text-align:left; border-radius:12px; border-left:4px solid #0284c7; padding:14px; box-shadow:0 2px 5px rgba(0,0,0,0.03); background:#ffffff;">
            <h4 style="font-size:13px; color:#0f2b5c; margin:0 0 10px 0; font-weight:800;"><i class="fa-solid fa-building-columns"></i> Identidad Comercial</h4>
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Nombre del Sistema / Negocio</label>
            <input type="text" name="empresa_nombre" value="{empresa_nombre}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; margin-bottom:10px; font-size:12px;">
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">WhatsApp Emisor Central</label>
            <input type="text" name="empresa_telefono" value="{empresa_telefono}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; font-size:12px;">
        </div>

        <!-- 🟢 SECCIÓN 2: MATEMÁTICAS Y CRÉDITOS -->
        <div class="card" style="text-align:left; border-radius:12px; border-left:4px solid #10b981; padding:14px; box-shadow:0 2px 5px rgba(0,0,0,0.03); background:#ffffff;">
            <h4 style="font-size:13px; color:#0f2b5c; margin:0 0 10px 0; font-weight:800;"><i class="fa-solid fa-calculator"></i> Reglas de Negocio</h4>
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Interés por Defecto (%) en Simulador</label>
            <input type="number" step="any" name="interes_defecto" value="{interes_defecto}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; margin-bottom:10px; font-size:12px;">
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Monto de Caja Base Inicial ($)</label>
            <input type="number" step="any" name="caja_base" value="{caja_base}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; font-size:12px;">
        </div>

        <!-- 🟢 SECCIÓN 3: CONFIGURACIÓN DEL TICKET DIGITAL -->
        <div class="card" style="text-align:left; border-radius:12px; border-left:4px solid #f59e0b; padding:14px; box-shadow:0 2px 5px rgba(0,0,0,0.03); background:#ffffff;">
            <h4 style="font-size:13px; color:#0f2b5c; margin:0 0 10px 0; font-weight:800;"><i class="fa-solid fa-receipt"></i> Personalización de Recibos</h4>
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Encabezado del Ticket de Cobro</label>
            <input type="text" name="ticket_encabezado" value="{ticket_encabezado}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; font-size:12px;">
        </div>

        <!-- 🟢 SECCIÓN 4: SEGURIDAD Y ACCESO CRIPTOGRÁFICO -->
        <div class="card" style="text-align:left; border-radius:12px; border-left:4px solid #6366f1; padding:14px; box-shadow:0 2px 5px rgba(0,0,0,0.03); background:#ffffff;">
            <h4 style="font-size:13px; color:#0f2b5c; margin:0 0 10px 0; font-weight:800;"><i class="fa-solid fa-user-shield"></i> Seguridad del Operador</h4>
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Usuario del Sistema</label>
            <input type="text" name="nuevo_usuario" value="{usuario_actual}" required style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; margin-bottom:10px; font-size:12px;">
            
            <label style="font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">Nueva Contraseña (Dejar en blanco para conservar actual)</label>
            <input type="password" name="nueva_clave" placeholder="Escribe la nueva contraseña de seguridad" style="width:100%; padding:9px; border-radius:6px; border:1px solid #cbd5e1; font-size:12px;">
        </div>

        <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:13px; font-weight:bold; font-size:13px; border-radius:8px; box-shadow: 0 4px 6px rgba(16, 185, 129, 0.2);"><i class="fa-solid fa-cloud-arrow-up"></i> Guardar y Sincronizar Cambios</button>
    </form>

    <!-- 🟢 SECCIÓN 5: ACCIONES DE MANTENIMIENTO PREVENTIVO -->
    <div class="card" style="text-align:left; border-radius:12px; border-left:4px solid #64748b; padding:14px; margin-top:14px; box-shadow:0 2px 5px rgba(0,0,0,0.03); background:#ffffff;">
        <h4 style="font-size:13px; color:#0f2b5c; margin:0 0 10px 0; font-weight:800;"><i class="fa-solid fa-screwdriver-wrench"></i> Herramientas de Mantenimiento</h4>
        <button type="button" onclick="if(confirm('¿Reiniciar todos los estados del día? Los clientes saltados volverán a aparecer en ruta.')) alert('Estados de ruta diarios reiniciados con éxito.');" style="background:#475569; color:white; border:none; padding:12px; border-radius:8px; font-size:12px; font-weight:bold; width:100%; cursor:pointer;"><i class="fa-solid fa-arrow-rotate-left"></i> Resetear Estados de Cobro Diarios</button>
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_config)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_config))

@app.route("/api/configuracion/guardar_maestro", methods=["POST"])
def api_guardar_configuracion_maestra():
    if not session.get("autenticado"): 
        return redirect("/login")
        
    # Recolección de variables del formulario avanzado
    empresa_nombre = request.form.get("empresa_nombre", "").strip()
    empresa_telefono = request.form.get("empresa_telefono", "").strip()
    ticket_encabezado = request.form.get("ticket_encabezado", "").strip()
    interes_defecto = request.form.get("interes_defecto", "20").strip()
    caja_base = request.form.get("caja_base", "100.00").strip()
    nuevo_usuario = request.form.get("nuevo_usuario", "").strip()
    nueva_clave = request.form.get("nueva_clave", "").strip()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # Diccionario de llaves para bucle rápido de guardado masivo en Postgres
            maestro_datos = {
                "empresa_nombre": empresa_nombre,
                "empresa_telefono": empresa_telefono,
                "ticket_encabezado": ticket_encabezado,
                "interes_defecto": interes_defecto,
                "caja_base": caja_base,
                "usuario": nuevo_usuario
            }
            
            for llave, valor in maestro_datos.items():
                if valor:
                    cursor.execute(
                        "INSERT INTO configuracion (llave, valor) VALUES (%s, %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                        (llave, valor)
                    )
            
            # 🔐 Si escribiste una nueva clave, la encriptamos; si la dejaste vacía, no altera la anterior
            if nueva_clave:
                hash_clave = generate_password_hash(nueva_clave)
                cursor.execute(
                    "INSERT INTO configuracion (llave, valor) VALUES ('clave', %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                    (hash_clave,)
                )
                
            conn.commit()
            session["usuario"] = nuevo_usuario
    except Exception as e:
        print(f"Error crítico al guardar configuración maestra: {e}")
    finally:
        pass
        
    return redirect("/menu/configuracion")

@app.route("/api/configuracion/guardar_maestro", methods=["POST"])
def api_guardar_configuracion_global():
    if not session.get("autenticado"): 
        return redirect("/login")
        
    # Recolección de variables del formulario avanzado
    empresa_nombre = request.form.get("empresa_nombre", "").strip()
    empresa_telefono = request.form.get("empresa_telefono", "").strip()
    ticket_encabezado = request.form.get("ticket_encabezado", "").strip()
    interes_defecto = request.form.get("interes_defecto", "20").strip()
    caja_base = request.form.get("caja_base", "100.00").strip()
    nuevo_usuario = request.form.get("nuevo_usuario", "").strip()
    nueva_clave = request.form.get("nueva_clave", "").strip()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # Diccionario de llaves para bucle rápido de guardado masivo en Postgres
            maestro_datos = {
                "empresa_nombre": empresa_nombre,
                "empresa_telefono": empresa_telefono,
                "ticket_encabezado": ticket_encabezado,
                "interes_defecto": interes_defecto,
                "caja_base": caja_base,
                "usuario": nuevo_usuario
            }
            
            for llave, valor in maestro_datos.items():
                if valor:
                    cursor.execute(
                        "INSERT INTO configuracion (llave, valor) VALUES (%s, %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                        (llave, valor)
                    )
            
            # 🔐 Si escribiste una nueva clave, la encriptamos de forma segura
            if nueva_clave:
                hash_clave = generate_password_hash(nueva_clave)
                cursor.execute(
                    "INSERT INTO configuracion (llave, valor) VALUES ('clave', %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                    (hash_clave,)
                )
                
            conn.commit()
            session["usuario"] = nuevo_usuario
    except Exception as e:
        print(f"Error crítico al guardar configuración maestra: {e}")
    finally:
        pass
        
    return redirect("/menu/configuracion")

@app.route("/api/clientes/lista_negra/<int:id>")
def api_lista_negra(id):
    if not session.get("autenticado"): 
        return "No autorizado"
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Sintaxis SQL limpia y directa
            cursor.execute("UPDATE clientes SET estado = 'Lista Negra' WHERE id = %s", (id,))
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
            respuesta.headers.add("Access-Control-Allow-Origin", "*")
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
    
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT llave, valor FROM configuracion WHERE llave IN ('usuario', 'cobrador_nombre', 'cobrador_cedula', 'cobrador_celular', 'cobrador_correo')")
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

@app.route("/api/perfil/guardar")
def api_guardar_perfil():
    if not session.get("autenticado"): 
        return jsonify({"status": "error"})
        
    nuevo_usuario = request.args.get("usuario")
    nueva_clave = request.args.get("clave")
    nom = request.args.get("nombre")
    ced = request.args.get("cedula")
    cel = request.args.get("celular")
    cor = request.args.get("correo")
    
    conn = get_db()
    with conn.cursor() as cursor:
        # 🛡️ MODO ELIMINACIÓN Y COCCIÓN REAL: Limpia duplicados fantasmas en Neon
        for llave_conf, valor_conf in [
            ('usuario', nuevo_usuario),
            ('cobrador_nombre', nom),
            ('cobrador_cedula', ced),
            ('cobrador_celular', cel),
            ('cobrador_correo', cor)
        ]:
            # Primero borramos cualquier rastro previo para evitar conflictos de filas duplicadas sin índice
            cursor.execute("DELETE FROM configuracion WHERE llave = %s", (llave_conf,))
            # Insertamos el valor fresco enviado desde el celular
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES (%s, %s)", (llave_conf, valor_conf))

        if nueva_clave and nueva_clave.strip() != "":
            clave_encriptada = generate_password_hash(nueva_clave)
            cursor.execute("UPDATE configuracion SET valor = %s WHERE llave = 'clave'", (clave_encriptada,))
    conn.commit()
    
    session["usuario"] = nuevo_usuario
    return jsonify({"status": "ok"})

@app.route("/menu/pagos_hoy")
def seccion_pagos_hoy():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    hoy_str = date.today().isoformat()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Traemos los recaudos individuales de hoy
            cursor.execute(
                """
                SELECT p.id AS pago_id, c.id AS cliente_id, c.nombre, p.numero, p.valor_pagado
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.fecha_pago_real = %s AND p.valor_pagado > 0
                ORDER BY c.nombre ASC, p.numero ASC
                """, (hoy_str,)
            )
            pagos_individuales = cursor.fetchall()

            # ❌ Obtener todos los clientes que fueron SALTADOS hoy por el cobrador
            cursor.execute(
                """
                SELECT id AS cliente_id, nombre, saltado_hoy
                FROM clientes
                WHERE fecha_gestion = %s AND estado = 'Activo' AND id NOT IN (
                    SELECT DISTINCT cliente_id FROM pagos WHERE fecha_pago_real = %s
                )
                """, (hoy_str, hoy_str)
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

@app.route("/api/revertir_gestion/<string:tipo>/<string:id_str>")
def api_revertir_gestion(tipo, id_str):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if tipo == "pago":
                pago_ids = [int(x) for x in id_str.split(",") if x.strip()]
                cliente_afectado = None
                for p_id in pago_ids:
                    cursor.execute(
                        "UPDATE pagos SET pagado = 0, valor_pagado = 0.00, fecha_pago_real = '' WHERE id = %s RETURNING cliente_id",
                        (p_id,)
                    )
                    res = cursor.fetchone()
                    if res:
                        cliente_afectado = res["cliente_id"] if isinstance(res, dict) else res[0]
                        cursor.execute("UPDATE clientes SET estado = 'Activo', fecha_gestion = '' WHERE id = %s", (cliente_afectado,))
                
                if cliente_afectado:
                    cursor.execute(
                        "DELETE FROM balance_movimientos WHERE tipo = 'Entrada' AND categoria = 'Cobranza' AND fecha = %s AND concepto LIKE %s",
                        (hoy_str, f"%(ID:{cliente_afectado})%")
                    )

            elif tipo == "salto":
                cliente_id = int(id_str)
                cursor.execute(
                    "UPDATE clientes SET saltado_hoy = GREATEST(0, saltado_hoy - 1), fecha_gestion = '' WHERE id = %s",
                    (cliente_id,)
                )
        conn.commit()
    except Exception as e:
        print(f"Error crítico al revertir gestión del día: {e}")
    finally:
        pass
        
    return jsonify({"status": "ok"})

@app.route("/api/clientes/reactivar/<int:cliente_id>")
def api_reactivar_cliente(cliente_id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Cambia el estado a Activo, limpia marcas viejas y lo reinserta al final de la ruta del día
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes WHERE estado = 'Activo'")
            max_orden = int(cursor.fetchone()[0])
            
            cursor.execute(
                """
                UPDATE clientes 
                SET estado = 'Activo', 
                    fecha_gestion = '', 
                    saltado_hoy = 0, 
                    enrutado_forzado = 1,
                    orden = %s 
                WHERE id = %s
                """, (max_orden + 1, cliente_id)
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
        
    efectivo_sistema = Decimal(request.form.get("efectivo_sistema", "0.00"))
    efectivo_real = Decimal(request.form.get("efectivo_real", "0.00"))
    
    diferencia = efectivo_real - efectivo_sistema
    if diferencia == 0:
        detalle_status = f"Cuadrado Perfecto ($0.00). Real: ${float(efectivo_real):.2f}"
    elif diferencia > 0:
        detalle_status = f"Sobrante de ${float(diferencia):.2f}. Real: ${float(efectivo_real):.2f}"
    else:
        detalle_status = f"FALTANTE DE ${float(abs(diferencia)):.2f}. Real: ${float(efectivo_real):.2f}"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # Recuperamos la fecha de la jornada que se está cerrando
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'fecha_jornada_activa'")
            row_j = cursor.fetchone()
            fecha_cierre_real = row_j["valor"] if (row_j and row_j["valor"]) else date.today().isoformat()

            # Sello inmutable de la jornada en Neon Cloud
            cursor.execute(
                "INSERT INTO configuracion (llave, valor) VALUES (%s, %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                (f"cierre_caja_{fecha_cierre_real}", detalle_status)
            )
            
            # Avanzamos la jornada activa al día siguiente o la limpiamos para el próximo turno
            proxima_jornada = (date.today() + timedelta(days=1)).isoformat() if fecha_cierre_real == date.today().isoformat() else date.today().isoformat()
            cursor.execute(
                "INSERT INTO configuracion (llave, valor) VALUES ('fecha_jornada_activa', %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                (proxima_jornada,)
            )
            
            # Cierre automático integrado: Bloquea el acceso hasta nueva autorización
            cursor.execute(
                "INSERT INTO configuracion (llave, valor) VALUES ('caja_autorizada', '0') ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor"
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico en el asiento del cierre: {e}")
    finally:
        pass
        
    # 🔒 ORDEN MÁXIMA DE SEGURIDAD FINANCIERA: 
    # Destruimos los accesos de la sesión de inmediato y redireccionamos a la página de Login
    session.clear()
    
    return redirect("/login")

if __name__ == "__main__":
    app.run(debug=True)
    # Servidor local configurado para ejecutarse en el puerto estricto 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
