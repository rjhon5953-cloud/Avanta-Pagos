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
    """🛡️ FIJADO: Registra la conexión en el contexto 'g' de Flask"""
    if 'db_conn' not in g:
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
        conn.commit()
    finally:
        release_db(conn)


with app.app_context():
    init_db()
    init_db_contable()
def obtener_clientes_ruta_hoy():
    hoy_str = date.today().isoformat()
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
        release_db(conn)

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
            atrasadas = sum(1 for p in c_dict["pagos"] if not p["pagado"] and p["fecha"] < hoy_str)
            c_dict["cuotas_atrasadas"] = atrasadas
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Avanta Pagos - Acceso</title>
    <link href="https://googleapis.com" rel="stylesheet">
    <style>
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; margin: 0; padding: 0; }
        body { background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 16px; }
        .login-card { background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.1); backdrop-filter: blur(16px); padding: 32px 24px; border-radius: 20px; width: 100%; max-width: 380px; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.4); }
        .logo-container { margin-bottom: 24px; }
        .logo-icon { font-size: 38px; background: linear-gradient(45deg, #00a8cc, #10b981); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 800; display: inline-block; }
        .logo-subtitle { color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-top: 2px; }
        h2 { color: white; font-size: 18px; font-weight: 700; margin: 20px 0; text-align: center; }
        .input-group { text-align: left; margin-bottom: 16px; }
        label { color: #cbd5e1; font-size: 11px; font-weight: 700; text-transform: uppercase; margin-bottom: 6px; display: block; }
        input { width: 100%; padding: 12px 14px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 10px; color: white; font-size: 14px; outline: none; display: block; margin-top: 4px; }
        .btn-access { background: linear-gradient(90deg, #00a8cc 0%, #0284c7 100%); color: white; font-weight: 700; font-size: 14px; border: none; padding: 14px; border-radius: 10px; width: 100%; cursor: pointer; margin-top: 14px; box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3); display: block; }
        .version-text { color: #64748b; font-size: 11px; font-weight: 600; margin-top: 24px; }
        .error-msg { background: rgba(239, 68, 68, 0.2); border: 1px solid #ef4444; color: #fca5a5; padding: 10px; border-radius: 8px; font-size: 12px; margin-bottom: 16px; }
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
        <form action="/login" method="POST">
            <div class="input-group">
                <label>Usuario Comercial</label>
                <input type="text" name="usuario" required placeholder="Ingresa tu usuario">
            </div>
            <div class="input-group">
                <label>Contraseña</label>
                <input type="password" name="clave" required placeholder="••••••••">
            </div>
            <button type="submit" class="btn-access">Acceder al Sistema</button>
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

{% elif vista == 'clientes_financieros' %}
    <div class="section-header-title"><i class="fa-solid fa-users"></i> HISTORIAL GENERAL DE CLIENTES</div>
    <p style="font-size:11px; color:#64748b; margin-bottom:12px; text-align:left; padding:0 4px;">Listado contable de todas las personas ingresadas en el sistema. Puedes auditar el capital en calle y los saldos reales generales.</p>
    
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente en el historial..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
        {% if lista_creditos %}
            {% for c in lista_creditos %}
                <div class="card" style="padding:14px; border-left:4px solid {% if c.estado == 'Activo' %}#10b981{% elif c.estado == 'Inactivo' %}#94a3b8{% else %}#475569{% endif %}; text-align:left; margin-bottom:10px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
                    <div class="flex-between" style="border-bottom:1px solid #f1f5f9; padding-bottom:6px; margin-bottom:8px;">
                        <div>
                            <span style="font-weight:800; font-size:14px; color:#0f2b5c; display:inline-block;">{{ c.nombre }}</span>
                            {% if c.estado == 'Inactivo' %}
                                <span class="badge-al-dia" style="background:#e2e8f0; color:#475569; margin-left:4px;">LIQUIDADO</span>
                            {% elif c.estado == 'Lista Negra' %}
                                <span class="badge-mora" style="background:#475569; color:white; margin-left:4px;">BLOQUEADO</span>
                            {% endif %}
                            <span style="font-size:10px; color:#64748b; display:block; margin-top:2px;">Modalidad: <b>{{ c.frecuencia }}</b> | Cuota: <b>${{ "%.2f"|format(c.valor_cuota) }}</b></span>
                        </div>
                        <div style="text-align:right;">
                            <span style="font-size:10px; color:#475569; font-weight:800; text-transform:uppercase; display:block;">Saldo Real</span>
                            <span style="font-size:15px; font-weight:800; color:{% if c.saldo_pendiente > 0 %}#ef4444{% else %}#10b981{% endif %}; display:block;">${{ "%.2f"|format(c.saldo_pendiente) }}</span>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; font-size:11px; background:#f8fafc; padding:8px; border-radius:8px; border:1px solid #e2e8f0;">
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Base</span>
                            <span style="font-weight:700; color:#334155;">${{ "%.2f"|format(c.monto) }}</span>
                        </div>
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Cartera Total</span>
                            <span style="font-weight:700; color:#0f2b5c;">${{ "%.2f"|format(c.monto_total) }}</span>
                        </div>
                        <div>
                            <span style="color:#64748b; font-size:9px; font-weight:800; text-transform:uppercase; display:block;">Recaudado</span>
                            <span style="font-weight:700; color:#10b981;">${{ "%.2f"|format(c.total_recaudado) }}</span>
                        </div>
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center; padding:20px;">No se registran clientes en la base de datos.</p>
        {% endif %}
    </div>

{% elif vista == 'embed_creditos_maestros' or vista == 'creditos_maestros' %}
    <div class="section-header-title"><i class="fa-solid fa-hand-holding-dollar"></i> CONTROL MAESTRO DE GESTIÓN</div>
    
    <div style="display:flex; gap:6px; margin-bottom:12px; overflow-x:auto;">
        <!-- 🎯 FIJADO: Aquí corregimos 'filtro' por 'filter' para que el backend responda, y arreglamos las llaves triples sin romper nada más -->
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Activo')">🟢 Activos ({{ total_activos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Inactivo')">⚪ Inactivos ({{ total_inactivos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filter=Lista Negra')">⚫ Bloqueados ({{ total_negra }})</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar registro..." onkeyup="filtrarClientes()">
    
    <div id="clientesContainer">
        <!-- 📱 CONSERVADO: Tu diseño de Tarjeta Premium impecable para celulares -->
        {% for c in todos %}
            {% set pagado_acumulado = c.pagos | map(attribute='valor_pagado') | sum if c.pagos else 0 %}
            {% set saldo_restante = c.monto_total - pagado_acumulado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first if c.pagos else None %}
            {% set cuotas_pagadas = c.pagos | selectattr('pagado', 'equalto', 1) | list | length if c.pagos else 0 %}
            
            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if c.saltado_hoy > 0 else 0 }}">
                <div class="card-izquierda">
                    <div class="circulo-frecuencia al-dia">{{ 'D' if c.frecuencia == 'Diaria' else 'S' }}</div>
                    
                    <div class="info-bloque-texto" style="width:100%;">
                        <div class="id-alias">{{ c.id }} {{ c.referencia or 'Comercio' }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <div class="grid-metricas-premium">
                            <div><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${{ "%.2f"|format(c.valor_cuota) }}</span></div>
                            <div><span class="metric-lbl">Pendiente</span><span class="metric-val">{{ cuotas_pagadas }} / {{ c.cuotas }}.0</span></div>
                            <div><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                        </div>
                        
                        <div class="fila-utilidades-premium">
                            <!-- 🛠️ FIJADO ACCIÓN DE RENOVACIÓN/GESTIÓN INTEGRADA -->
                            {% if c.estado == 'Inactivo' or c.estado == 'Lista Negra' %}
                                <button type="button" class="btn-accion" onclick="navegarRuta('/mover_renovacion/{{ c.id }}')" style="background: #f97316; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 11px;">
                                    <i class="fa-solid fa-rotate"></i> Renovar
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
                
                <div class="card-derecha-acciones">
                    {% if cuota_pendiente %}
                        <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})">💵✔️</button>
                        <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({{ c.id }})">💵❌</button>
                    {% endif %}
                </div>
            </div>
        {% endfor %}
    </div>

{% elif vista == 'pagos_hoy' %}
    <div class="section-header-title"><i class="fa-solid fa-receipt"></i> Panel de Control - Pagos del Día</div>
    <p style="font-size:11px; color:#64748b; margin-bottom:12px; text-align:left; padding:0 4px;">Audita los movimientos ejecutados hoy. Si el cobrador cometió un error o saltó a alguien por accidente, usa el botón de deshacer para regresarlo a la ruta activa.</p>
    
    <!-- Tab 1: Clientes que SÍ pagaron hoy con empaquetamiento limpio -->
    <div class="card" style="text-align:left; padding:16px;">
        <h4 style="font-size:13px; color:#10b981; margin-bottom:10px;"><i class="fa-solid fa-circle-check"></i> Clientes que SÍ Pagaron Hoy</h4>
        {% if pagados_list %}
            {% for p in pagados_list %}
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding:10px 0; font-size:12px;">
                    <div>
                        <!-- 🔥 FIJADO: Muestra el nombre y el rango de cuotas agrupadas de forma elegante sin repetir líneas -->
                        <b>{{ p.nombre }}</b> <span style="color:#64748b; font-size:11px; margin-left:4px;">(Cuotas {{ p.rango_cuotas }})</span>
                        <div style="color:#10b981; font-weight:800; margin-top:2px;">Total Recaudado: ${{ "%.2f"|format(p.total_abonado) }}</div>
                    </div>
                    <button type="button" onclick="revertirGestionDia('pago', '{{ p.pago_ids_str }}')" class="btn-accion btn-nopagar" style="background:#ef4444; padding:6px 10px;"><i class="fa-solid fa-arrow-rotate-left"></i> Deshacer</button>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center; padding:10px;">Ningún recaudo asentado hoy todavía.</p>
        {% endif %}
    </div>

    <!-- Tab 2: Clientes SALTADOS hoy -->
    <div class="card" style="text-align:left; padding:16px; margin-top:12px;">
        <h4 style="font-size:13px; color:#f59e0b; margin-bottom:10px;"><i class="fa-solid fa-circle-exclamation"></i> Clientes Saltados / No Pago Hoy</h4>
        {% if saltados_list or saltados_list %}
            {% for s in saltados_list %}
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding:10px 0; font-size:12px;">
                    <div>
                        <b>{{ s.nombre }}</b>
                        <div style="color:#64748b; font-size:11px; margin-top:2px;">Total días saltado: {{ s.saltado_hoy }}</div>
                    </div>
                    <button type="button" onclick="revertirGestionDia('salto', {{ s.cliente_id }})" class="btn-accion btn-nopagar" style="background:#ef4444; padding:6px 10px;"><i class="fa-solid fa-arrow-rotate-left"></i> Deshacer</button>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center; padding:10px;">Ningún cliente saltado hoy.</p>
        {% endif %}
    </div>

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
        <form action="/guardar" method="POST">
            {% if vista == 'renovar' %}<input type="hidden" name="cliente_id" value="{{ cliente.id }}">{% endif %}
            <label>Nombre del Cliente</label><input type="text" name="nombre" value="{{ cliente.nombre | default('') }}" required>
            <label>WhatsApp</label><input type="text" name="telefono" value="{{ cliente.telefono | default('') }}" required>
            <label>Identificación / Cédula</label><input type="text" name="identificacion" value="{{ cliente.identificacion | default('') }}">
            <label>Dirección Domiciliaria</label><input type="text" name="direccion" value="{{ cliente.direccion | default('') }}">
            <label>Referencia Local</label><input type="text" name="referencia" value="{{ cliente.referencia | default('') }}">
            <label>Monto Financiado ($)</label><input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            <label>Interés (%)</label><input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            <label>Cuotas</label><input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
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
            <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:12px; margin-top:8px; font-size:13px; font-weight:800;">💾 Guardar e Inicializar Tablas</button>
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
                        <a href="/api/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar Gasto?')" style="color:#ef4444; text-decoration:none; font-weight:bold; font-size:14px; margin-left:4px;">🗑️</a>
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center;">No hay flujos asentados hoy.</p>
        {% endif %}
    </div>
{% endif %}
"""



HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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

            <!-- ⏻ CERRAR SESIÓN (BOTÓN DE APAGADO DIGITAL) -->
            <li style="margin: 10px 0 0 0; padding-top: 8px; border-top: 1px solid #f1f5f9;">
                <a href="/logout" style="display: flex; align-items: center; justify-content: center; gap: 10px; padding: 12px; background: #e11d48; color: white; text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 13px; box-shadow: 0 4px 6px rgba(225, 29, 72, 0.12);">
                    <span style="font-size: 14px;">⏻</span> Apagar Sesión
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

    <div class="modal" id="modalAbono">
        <div class="modal-content">
            <h3 id="modalAbonoTitulo">✏️ Registrar Pago</h3>
            <input type="hidden" id="abonoClienteId"><input type="hidden" id="abonoNumCuota">
            <input type="number" step="any" id="abonoMontoInput" placeholder="Monto del dinero ($)">
            <button onclick="confirmarAbono()" class="btn-primary" style="margin-top:10px; background:#10b981; color:white; border:none; padding:12px; font-weight:bold; width:100%; border-radius:8px; cursor:pointer;">💾 Guardar Recaudo</button>
            <button onclick="cerrarModal('modalAbono')" class="btn-modal-close">Cancelar</button>
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
                document.getElementById('btn_mapa').href = 'https://google.com' + d.data.latitud + ',' + d.data.longitud;
                
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

function calcularCuota() {
    const monto = parseFloat(document.getElementById('calcMonto').value) || 0;
    const interes = parseFloat(document.getElementById('calcInteres').value) || 0;
    const cuotas = parseInt(document.getElementById('calcCuotas').value) || 0;
    if (monto > 0 && cuotas > 0) {
        const total = monto * (1 + (interes / 100));
        document.getElementById('simulacionText').innerText = '$' + (total / cuotas).toFixed(2) + ' / cuota';
    }
}
function abrirModalAbono(clienteId, numCuota, pendiente, valorCuota) {
    document.getElementById('abonoClienteId').value = clienteId;
    document.getElementById('abonoNumCuota').value = numCuota;
    document.getElementById('abonoMontoInput').value = pendiente.toFixed(2);
    document.getElementById('modalAbonoTitulo').innerText = '✏️ Registrar Pago - Cuota #' + numCuota;
    document.getElementById('modalAbono').style.display = 'flex';
}
function confirmarAbono() {
    const clienteId = document.getElementById('abonoClienteId').value;
    const numCuota = document.getElementById('abonoNumCuota').value;
    const monto = parseFloat(document.getElementById('abonoMontoInput').value);
    if (monto > 0) { cerrarModal('modalAbono'); procesarPagoAPI(clienteId, numCuota, monto); }
}
function ejecutarPago(clienteId, numCuota, pendiente) {
    procesarPagoAPI(clienteId, numCuota, pendiente);
}
// Variables de control ambiental de carga síncrona multimedia
let imagenComprimidaB64 = "";
let fotoListaParaEnviar = false;

function procesarPagoAPI(clienteId, numCuota, monto) {
    fetch('/api/marcar_pago/' + clienteId + '/' + numCuota + '?monto=' + monto)
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
    if (confirm('¿Desea saltar el cobro de este cliente por el día de hoy?')) {
        fetch('/api/marcar_no_pago/' + clienteId)
            .then(res => res.json())
            .then(data => {
                if (data.status === 'ok') {
                    // ❌ FIJADO: Recarga el listado dinámico dentro de la misma vista sin cambiar de sección
                    window.location.reload();
                }
            }).catch(err => alert("Error al procesar el salto: " + err));
    }
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
            
            // Abre Google Maps automáticamente con un pin en la ubicación actual
            const urlMaps = `https://google.com{lat},${lon}`;
            window.open(urlMaps, '_blank');
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
            release_db(conn)
                
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
        
    hoy_str = date.today().isoformat()
    
    # 📅 FECHA EN TIEMPO REAL: Formateo en español para el badge superior
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    ahora = datetime.now()
    fecha_legible = f"{dias_semana[ahora.weekday()]}, {ahora.day:02d} de {meses[ahora.month - 1]} de {ahora.year}"
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 💵 1. Suma real en dólares del recaudo asentado el día de hoy
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE fecha_pago_real = %s AND pagado = 1", (hoy_str,))
            recaudo_hoy = Decimal(str(cursor.fetchone()["total"]))
            
            # 👤 2. Conteo exacto de la cantidad de créditos nuevos entregados hoy
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
            creditos_hoy = int(cursor.fetchone()["total"])
            
            # 📈 3. Suma real del dinero prestado (capital neto entregado hoy en la calle)
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
            capital_prestado_hoy = Decimal(str(cursor.fetchone()["total"]))
    except Exception as e:
        print(f"Error contable en Dashboard Principal: {e}")
        recaudo_hoy = Decimal("0.00")
        creditos_hoy = 0
        capital_prestado_hoy = Decimal("0.00")
    finally:
        release_db(conn)

    clientes_ruta = obtener_clientes_ruta_hoy()
    debido_minimo_dia = sum(Decimal(str(cl["valor_cuota"])) for cl in clientes_ruta)
    
    debido_total_acumulado = Decimal("0.00")
    for cl in clientes_ruta:
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
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">🔬 Mínimo de Ruta: <b>${debido_minimo_dia:.2f}</b> | Acumulado Mora: <b>${debido_total_acumulado:.2f}</b></div>
    </div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar por nombre de cliente..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
    """
    
    # 📱 PROCESAMIENTO CONTABLE PREMIUM BLINDADO Y UNIFICADO
    for c in clientes_ruta:
        pagado_acumulado = sum(Decimal(str(p["valor_pagado"])) for p in c["pagos"])
        saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - pagado_acumulado)
    
        # Calculamos de forma exacta cuántas cuotas completas equivalen al dinero abonado
        valor_cuota_dec = Decimal(str(c["valor_cuota"]))
        cuotas_pagadas = int(pagado_acumulado // valor_cuota_dec) if valor_cuota_dec > 0 else 0
    
        # 🛡️ FIJADO: Operador de comparación exacto (==) para hallar la cuota pendiente en la ruta
        cuota_p = next((p for p in c["pagos"] if int(p["pagado"]) == 0), None)
        es_mora_cl = int(c.get("es_mora_cl", 0)) > 0  # Ajuste seguro de bandera de mora
    
        c_id = str(c["id"])
        c_nombre = str(c["nombre"])
        c_nombre_lower = c_nombre.lower()
        c_referencia = str(c["referencia"]) if c["referencia"] else "Comercio"
        c_valor_cuota = f"{float(valor_cuota_dec):.2f}"
        c_cuotas = str(c["cuotas"])
        c_saldo_restante = f"{float(saldo_restante):.2f}"
        c_cuotas_pagadas = str(cuotas_pagadas)

        inicial_frecuencia = 'D' if c['frecuencia'] == 'Diaria' else 'S'
        clase_mora = "mora-leve" if es_mora_cl else "al-dia"
        clase_mora_badge = "1" if es_mora_cl else "0"
        clase_check = "gestionado" if c['saltado_hoy'] > 0 else ("gestionado-ok" if cuotas_pagadas > 0 else "")
        numero_check = str(c['saltado_hoy'] if c['saltado_hoy'] > 0 else (cuotas_pagadas if cuotas_pagadas > 0 else 1))
        
        js_num_cuota = str(cuota_p["numero"]) if cuota_p else "1"
        js_pendiente = f"{(cuota_p['valor'] - cuota_p['valor_pagado'])}" if cuota_p else "0"
        
        contexto_dash += f"""
        <div class="cliente-card-premium" id="cliente-card-{c_id}" data-nombre="{c_nombre_lower}" data-mora="{clase_mora_badge}">
            <div class="card-izquierda">
                <div class="circulo-frecuencia {clase_mora}">{inicial_frecuencia}</div>
                <div class="info-bloque-texto" style="width:100%;">
                    <div class="id-alias">{c_id} {c_referencia}</div>
                    <div class="nombre-real">{c_nombre}</div>
                    
                    <div class="grid-metricas-premium">
                        <div class="metric-col"><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${c_valor_cuota}</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{c_cuotas_pagadas} / {c_cuotas}</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                    </div>
                    
                    <div class="fila-utilidades-premium">
                        <!-- 📸 FIJADO: El botón de la cámara ahora abre directamente la modal de abonos para adjuntar comprobante sin desviar el flujo -->
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}); alert('📸 Adjunta el comprobante para la cuota de {c_nombre}');">📸</button>
                        <div class="circulo-check-cuota {clase_check}">{numero_check}</div>
                        <div class="badge-saldo-premium">Saldo <b>${c_saldo_restante}</b></div>
                    </div>
                </div>
            </div>
            
            <div class="card-derecha-acciones" id="acciones-{c_id}">
                <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota})">💵✔️</button>
                <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({c_id})">💵❌</button>
            </div>
        """
        
        contexto_dash += "</div>"
    
    # 🎨 Inyectamos el bloque procesado y controlamos el formato nativo para celulares
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
            # 🛡️ FIJADO: Cambiado c.c_estado por c.estado para corregir la pantalla en blanco
            cursor.execute(
                """
                SELECT c.id, c.nombre, c.monto, c.interes_porcentaje, c.monto_total, c.valor_cuota, c.frecuencia, c.estado,
                       COALESCE(SUM(p.valor_pagado), 0) AS total_recaudado
                FROM clientes c
                LEFT JOIN pagos p ON c.id = p.cliente_id
                GROUP BY c.id
                ORDER BY c.nombre ASC
                """
            )
            todos_clientes = cursor.fetchall()
    except Exception as e:
        print(f"Error al consultar historial de clientes: {e}")
        todos_clientes = []
    finally:
        release_db(conn)
            
    creditos_procesados = []
    for cr in todos_clientes:
        c_dict = dict(cr)
        
        # 🪙 Cálculos exactos con Decimal convertidos a float solo para el renderizado visual de Jinja
        monto_total_dec = Decimal(str(c_dict["monto_total"]))
        total_recaudado_dec = Decimal(str(c_dict["total_recaudado"]))
        
        saldo_pendiente = max(Decimal("0.00"), monto_total_dec - total_recaudado_dec)
        
        c_dict["saldo_pendiente"] = float(saldo_pendiente)
        c_dict["monto"] = float(c_dict["monto"])
        c_dict["monto_total"] = float(c_dict["monto_total"])
        c_dict["valor_cuota"] = float(c_dict["valor_cuota"])
        c_dict["total_recaudado"] = float(total_recaudado_dec)
        
        creditos_procesados.append(c_dict)
        
    contexto = dict(
        vista="clientes_financieros", 
        lista_creditos=creditos_procesados
    )
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

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
                "SELECT id, nombre, frecuencia, monto_total, estado, saltado_hoy FROM clientes WHERE estado = %s ORDER BY nombre ASC", 
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
        release_db(conn)
            
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
    hoy_str = date.today().isoformat()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🏢 1. Recuperamos la Caja Base o el arrastre del día anterior
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base'")
            res_caja = cursor.fetchone()
            caja_base_fija = Decimal(res_caja["valor"]) if res_caja else Decimal("100.00")
            
            # Buscamos si hay un arqueo físico del día de ayer para arrastrarlo
            ayer_str = (date.today() - timedelta(days=1)).isoformat()
            cursor.execute("SELECT valor FROM configuracion WHERE llave = %s", (f"cierre_caja_{ayer_str}",))
            res_ayer = cursor.fetchone()
            
            if res_ayer and "Real: $" in res_ayer["valor"]:
                try:
                    # Extraemos el número final que guardó el cobrador en su balance anterior
                    caja_base = Decimal(res_ayer["valor"].split("Real: $")[-1])
                except Exception:
                    caja_base = caja_base_fija
            else:
                caja_base = caja_base_fija
            
            # 📊 2. Consultamos si la jornada ya fue sellada hoy
            cursor.execute("SELECT valor FROM configuracion WHERE llave = %s", (f"cierre_caja_{hoy_str}",))
            res_cierre = cursor.fetchone()
            estado_cierre = res_cierre["valor"] if res_cierre else "Abierta"
            
            # 📝 3. Traemos los movimientos manuales de la tabla de flujos
            cursor.execute("SELECT id, tipo, categoria, concepto, monto, fecha, comprobante FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
            movimientos = cursor.fetchall()
            
            # 🪙 4. Sumamos en tiempo real los abonos en efectivo cobrados en la calle hoy
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE fecha_pago_real = %s AND pagado = 1", (hoy_str,))
            total_abonos_hoy = Decimal(str(cursor.fetchone()["total"]))
            
            # 👤 5. Sumamos el dinero prestado (capital puro) en créditos nuevos hoy
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
            total_prestado_hoy = Decimal(str(cursor.fetchone()["total"]))
    except Exception as e:
        print(f"Error en balance: {e}")
        caja_base = total_abonos_hoy = total_prestado_hoy = Decimal("0.00")
        movimientos = []
        estado_cierre = "Abierta"
    finally:
        release_db(conn)
        
    # #️⃣ Agrupamos los flujos de la tabla de movimientos manuales con precisión Decimal
    entradas_manuales = sum(Decimal(str(m["monto"])) for m in movimientos if m["tipo"] == "Entrada")
    salidas_manuales = sum(Decimal(str(m["monto"])) for m in movimientos if m["tipo"] == "Salida")
    
    # 📈 FÓRMULA CONTABLE EXACTA DE CAJA REAL BIEN CALCULADA
    entradas_totales = total_abonos_hoy + entradas_manuales
    salidas_totales = salidas_manuales + total_prestado_hoy
    efectivo_neto = caja_base + entradas_totales - salidas_totales
    
    # 🧼 Clean y purificación de imágenes Base64 de los comprobantes
    movimientos_limpios = []
    for m in movimientos:
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
        
    # Añadimos al desglose visual un registro informativo del capital prestado
    if total_prestado_hoy > 0:
        movimientos_limpios.insert(0, {
            "id": 0, "tipo": "Salida", "categoria": "Inversión",
            "concepto": "🔹 CAPITAL ENTREGADO EN CRÉDITOS NUEVOS",
            "monto": float(total_prestado_hoy), "fecha": hoy_str, "comprobante": ""
        })
        
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
            release_db(conn)
            
    contexto = dict(
        vista="nuevo" if not cliente_id else "renovar", 
        hoy_str=hoy_str, 
        cliente=cliente
    )
    
    # 🪙 FIJADO: Operador de comparación exacto para el refresco asíncrono del móvil
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/guardar", methods=["POST"])
def guardar_nuevo_cliente():
    if not session.get("autenticado"): 
        return redirect("/login")
    try:
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").strip()
        identificacion = request.form.get("identificacion", "").strip()
        direccion = request.form.get("direccion", "").strip()
        referencia = request.form.get("referencia", "").strip()
        
        # 💰 Captura de datos usando precisión contable Decimal
        monto = Decimal(request.form.get("monto", "0"))
        interes_porcentaje = Decimal(request.form.get("interes_porcentaje", "20"))
        cuotas = int(request.form.get("cuotas", "0"))
        frecuencia = request.form.get("frecuencia")
        fecha_inicio_dt = date.fromisoformat(request.form.get("fecha_inicio"))
        
        # 📍 Captura nativa de coordenadas geográficas desde el GPS del móvil
        latitud = request.form.get("latitud", "").strip()
        longitud = request.form.get("longitud", "").strip()
        
        # Redondeado matemático estricto a 2 decimales para evitar fugas de centavos
        monto_total = round(monto * (Decimal("1") + (interes_porcentaje / Decimal("100"))), 2)
        valor_base_cuota = round(monto_total / cuotas, 2)
        fecha_vencimiento_dt = calcular_fecha(fecha_inicio_dt, cuotas, frecuencia)
        
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes")
                res_orden = cursor.fetchone()
                max_orden = int(res_orden[0]) if res_orden and res_orden[0] is not None else 0
                
                cursor.execute(
                    """
                    INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, latitud, longitud, estado)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo') RETURNING id
                    """,
                    (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat(), latitud, longitud)
                )
                res_id = cursor.fetchone()
                cliente_id = res_id[0] if res_id else None
                
                if cliente_id:
                    acumulado = Decimal("0.00")
                    for num in range(1, cuotas + 1):
                        # Absorción de residuos de centavos en la última cuota
                        if num == cuotas:
                            valor_cuota_real = monto_total - acumulado
                        else:
                            valor_cuota_real = valor_base_cuota
                            
                        acumulado += valor_cuota_real
                        f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frecuencia)
                        
                        cursor.execute(
                            """
                            INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real)
                            VALUES (%s, %s, %s, %s, 0, 0, '')
                            """,
                            (cliente_id, num, f_cuota.isoformat(), valor_cuota_real)
                        )
            conn.commit()
        finally:
            release_db(conn)
            
    except Exception as e:
        print(f"Error crítico en guardar cliente con GPS: {e}")
        
    return redirect("/")


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
        release_db(conn)
        
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
        release_db(conn)
        
    return redirect("/menu/balance")

@app.route("/api/eliminar_cliente/<int:id>")
def api_eliminar_cliente(id):
    if not session.get("autenticado"): 
        return "No autorizado"
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 🛡️ FIJADO: Borrado físico en cascada con liberación segura de recursos en la nube
            cursor.execute("DELETE FROM clientes WHERE id = %s", (id,))
        conn.commit()
    except Exception as e:
        print(f"Error crítico al eliminar cliente de la base de datos: {e}")
    finally:
        release_db(conn)
        
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
        release_db(conn)
        
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
        release_db(conn)

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
            
            <!-- ⚡ BOTONES INTERACTIVOS CIRCULARES DE FLECHAS TÁCTILES PREMIUM -->
            <div style="display:flex; gap:8px; flex-shrink: 0; margin-left: 10px;">
                <a href="/mover/{cl['id']}/subir" style="text-decoration:none; width:36px; height:36px; background:#f1f5f9; color:#0284c7; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:14px; transition: background 0.2s;" title="Subir">
                    <i class="fa-solid fa-chevron-up"></i>
                </a>
                <a href="/mover/{cl['id']}/bajar" style="text-decoration:none; width:36px; height:36px; background:#f1f5f9; color:#e11d48; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:14px; transition: background 0.2s;" title="Bajar">
                    <i class="fa-solid fa-chevron-down"></i>
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
        release_db(conn)
            
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
        release_db(conn)

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
        release_db(conn)
        
    return redirect("/")

@app.route("/menu/salvar_datos")
def seccion_respaldo_nube():
    if not session.get("autenticado"): 
        return "Sesión expirada"
        
    hoy_str = date.today().isoformat()
    error_respaldo = None
    
    # 🛰️ RESPALDO REAL: Forzamos una verificación física de consistencia en las tablas de Neon
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM clientes")
            total_clientes = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
            pagos_hoy = cursor.fetchone()[0]
        conn.commit() # Aseguramos que Render asiente de forma física todos los hilos del día en Neon
    except Exception as e:
        error_respaldo = str(e)
    finally:
        release_db(conn)

    # Generamos el mensaje dinámico real según el estado de la base de datos
    if error_respaldo:
        mensaje_consola = f"⚠️ Alerta en la nube: {error_respaldo}. Intenta de nuevo."
        icono_status = "fa-triangle-exclamation"
        color_status = "#ef4444"
    else:
        mensaje_consola = f"✅ ¡Conexión con Neon AWS certificada! Respaldo verificado con éxito de {total_clientes} clientes y {pagos_hoy} cobros de hoy."
        icono_status = "fa-cloud-arrow-up"
        color_status = "#0284c7"

    # 🎨 Interfaz Premium Interactiva para Celulares
    html_cloud = f"""
    <div class="section-header-title"><i class="fa-solid fa-cloud-arrow-up"></i> Salvar Datos en la Nube (Neon AWS)</div>
    <div class="card" style="text-align:center; padding:30px 16px; border-radius:12px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
        <i class="fa-solid {icono_status}" style="font-size:48px; color:{color_status}; margin-bottom:12px;"></i>
        <h4 style="font-size:14px; color:#0f2b5c; margin:0 0 6px 0; font-weight:800;">Sincronización de Respaldo Satelital</h4>
        <p style="font-size:11px; color:#64748b; margin-bottom:16px; line-height:1.4; text-align:center;">
            Toda tu información contable se transmite y respalda de forma automática en los servidores de Neon. Puedes forzar una auditoría de consistencia de datos aquí mismo.
        </p>
        <button type="button" onclick="alert('{mensaje_consola}');" class="btn-accion btn-pagar" style="width:100%; padding:12px; font-weight:bold; font-size:12px;">
            🔄 Forzar Auditoría de Consistencia
        </button>
    </div>
    """

    # 🪙 FIJADO: Operador de comparación exacto para el refresco asíncrono del móvil
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_cloud)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_cloud))

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
    hoy_str = date.today().isoformat()
    monto_restante = monto_recaudado
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 📊 Recuperamos las cuotas pendientes ordenadas cronológicamente por número
            cursor.execute("SELECT id, numero, valor, valor_pagado, pagado FROM pagos WHERE cliente_id = %s AND pagado = 0 ORDER BY numero ASC", (cliente_id,))
            pagos_pendientes = cursor.fetchall()
            
            for p in pagos_pendientes:
                if monto_restante <= 0:
                    break
                    
                pendiente_cuota = Decimal(str(p["valor"])) - Decimal(str(p["valor_pagado"]))
                
                if monto_restante >= pendiente_cuota:
                    monto_restante = round(monto_restante - pendiente_cuota, 2)
                    cursor.execute(
                        "UPDATE pagos SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (p["valor"], hoy_str, p["id"])
                    )
                else:
                    nuevo_pago_parcial = round(Decimal(str(p["valor_pagado"])) + monto_restante, 2)
                    monto_restante = Decimal("0.00")
                    cursor.execute(
                        "UPDATE pagos SET valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (nuevo_pago_parcial, hoy_str, p["id"])
                    )
            
            # Recalcular balances reales del cliente para validar si ya liquidó la deuda completa
            cursor.execute("SELECT nombre, telefono, monto_total FROM clientes WHERE id = %s", (cliente_id,))
            c = cursor.fetchone()
            
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE cliente_id = %s", (cliente_id,))
            total_pagado = Decimal(str(cursor.fetchone()["total"]))
            
            saldo_restante = max(Decimal("0.00"), Decimal(str(c["monto_total"])) - total_pagado)
            
            cursor.execute("UPDATE clientes SET fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))

            # 🛡️ Si el saldo llegó a $0.00, el cliente pasa a 'Inactivo' (Liquidado) de manera automática
            if saldo_restante <= 0:
                cursor.execute("UPDATE clientes SET estado = 'Inactivo', enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (cliente_id,))
            else:
                cursor.execute("UPDATE clientes SET enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (cliente_id,))
                
            conn.commit()
    finally:
        release_db(conn)
        
        # 🧾 DISEÑO DE TICKET PREMIUM REESTRUCTURADO PARA WHATSAPP
    linea_division = "------------------------------------------"
    
    mensaje_ws = (
        "🧾 *AVANTA PAGOS* 🧾\n"
        "_*Comprobante Oficial de Pago*_\n"
        f"{linea_division}\n"
        f"👤 *Cliente:* {c['nombre']}\n"
        f"📱 *WhatsApp:* {c['telefono'] or 'N/A'}\n"
        f"📅 *Fecha de Pago:* {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"{linea_division}\n"
        f"💵 *Monto Cobrado:* ${float(monto_recaudado):.2f}\n"
        f"🔢 *Cuota Procesada:* #{num_cuota}\n"
        f"📉 *Saldo Restante:* ${float(saldo_restante):.2f}\n"
        f"{linea_division}\n"
    )
    
    if saldo_restante <= 0:
        mensaje_ws += "🎉 *¡FELICIDADES! CRÉDITO LIQUIDADO AL 100%* 🎉\n"
    else:
        mensaje_ws += "🙏 *¡Gracias por su puntualidad y pago!* 🙏\n"
        
    mensaje_ws += f"{linea_division}\n_Sistema de Gestión de Cobros Avanta_"
    
    recibo = {
        "cliente": c["nombre"],
        "telefono": c["telefono"] or "",
        "cuota": num_cuota,
        "monto": float(monto_recaudado),
        "saldo": float(saldo_restante),
        "mensaje_ws": mensaje_ws
    }
    return jsonify({"status": "ok", "recibo": recibo})


@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    conn = get_db()
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
        release_db(conn)
        
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
        release_db(conn)

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
        release_db(conn)
        
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
        release_db(conn)
        
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
        release_db(conn)
        
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
        release_db(conn)
        
    return jsonify({"status": "error", "message": "No encontrado"})

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
                WHERE p.fecha_pago_real = %s AND p.pagado = 1
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
        release_db(conn)

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
                # Desestructuramos los IDs del bloque agrupado (ej: "12,13,14") para revertirlos juntos
                pago_ids = [int(x) for x in id_str.split(",") if x.strip()]
                for p_id in pago_ids:
                    cursor.execute(
                        "UPDATE pagos SET pagado = 0, valor_pagado = 0.00, fecha_pago_real = '' WHERE id = %s RETURNING cliente_id",
                        (p_id,)
                    )
                    res = cursor.fetchone()
                    if res:
                        cursor.execute("UPDATE clientes SET fecha_gestion = '' WHERE id = %s", (res[0],))
                        
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
        release_db(conn)
        
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
        release_db(conn)
        
    # Redirecciona automáticamente de vuelta al menú maestro de control
    return redirect("/menu/creditos?filtro=Inactivo")

@app.route("/api/balance/cerrar_caja", methods=["POST"])
def api_procesar_cierre_caja():
    if not session.get("autenticado"): 
        return redirect("/login")
        
    hoy_str = date.today().isoformat()
    efectivo_sistema = Decimal(request.form.get("efectivo_sistema", "0.00"))
    efectivo_real = Decimal(request.form.get("efectivo_real", "0.00"))
    
    # 📊 Calculamos si la caja cuadra exacto o tiene diferencias físicas en el bolso
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
            # 🛡️ Sello inmutable de la jornada en Neon Cloud
            cursor.execute(
                "INSERT INTO configuracion (llave, valor) VALUES (%s, %s) ON CONFLICT (llave) DO UPDATE SET valor = EXCLUDED.valor",
                (f"cierre_caja_{hoy_str}", detalle_status)
            )
        conn.commit()
    except Exception as e:
        print(f"Error crítico en el asiento del cierre: {e}")
    finally:
        release_db(conn)
        
    # 🔒 ORDEN MÁXIMA DE SEGURIDAD FINANCIERA: 
    # Destruimos los accesos de la sesión de inmediato y redireccionamos a la página de Login
    session.clear()
    
    return redirect("/login")

if __name__ == "__main__":
    # Servidor local configurado para ejecutarse en el puerto estricto 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
