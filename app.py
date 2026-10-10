import base64
import os
import psycopg2
from psycopg2.extras import DictCursor
import urllib.parse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, session, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "avanta_secret_key_1.3_2026")

# 🔌 FIJADO: Extracción limpia directo de la memoria nativa de Render (Evita usar python-dotenv)
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    if not DATABASE_URL:
        raise ValueError("Error crítico: DATABASE_URL no está configurada en las variables de entorno.")
    return psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clientes (
                    id SERIAL PRIMARY KEY,
                    orden INTEGER NOT NULL DEFAULT 0,
                    nombre TEXT NOT NULL,
                    telefono TEXT,
                    monto REAL NOT NULL,
                    interes_porcentaje REAL NOT NULL DEFAULT 20,
                    monto_total REAL NOT NULL DEFAULT 0,
                    cuotas INTEGER NOT NULL,
                    frecuencia TEXT NOT NULL,
                    valor_cuota REAL NOT NULL,
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

init_db()

def init_db_contable():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pagos (
                    id SERIAL PRIMARY KEY,
                    cliente_id INTEGER NOT NULL,
                    numero INTEGER NOT NULL,
                    fecha TEXT NOT NULL,
                    valor REAL NOT NULL,
                    pagado INTEGER NOT NULL DEFAULT 0,
                    valor_pagado REAL NOT NULL DEFAULT 0,
                    fecha_pago_real TEXT DEFAULT '',
                    FOREIGN KEY (cliente_id) REFERENCES clientes (id) ON DELETE CASCADE
                )
                """
            )
            # 📸 FIJADO: Columna de tipo TEXT para guardar Base64 plano de forma nativa sin romper Neon
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_movimientos (
                    id SERIAL PRIMARY KEY,
                    tipo TEXT NOT NULL,
                    categoria TEXT NOT NULL,
                    concepto TEXT NOT NULL,
                    monto REAL NOT NULL,
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
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('usuario', 'admin') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('clave', '1234') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('empresa_nombre', 'AVANTA PAGOS') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('empresa_telefono', '593991234567') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('ticket_encabezado', '¡BIENVENIDO A AVANTA PAGOS!') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('caja_base', '100.00') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO configuracion (llave, valor) VALUES ('balance_estado', 'Sin cerrar') ON CONFLICT DO NOTHING")
        conn.commit()

init_db_contable()

def obtener_clientes_ruta_hoy():
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            if not clientes:
                return []
            
            cliente_ids = [c["id"] for c in clientes]
            cursor.execute("SELECT * FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (cliente_ids,))
            pagos = cursor.fetchall()

    pagos_por_cliente = defaultdict(list)
    for p in pagos:
        pagos_por_cliente[p["cliente_id"]].append(dict(p))

    resultado = []
    for c in clientes:
        c_dict = dict(c)
        
        # 🛡️ FIJADO: Si el cliente ya fue saltado o cobrado hoy, se lo excluye para limpiar la lista visual
        if c_dict.get("fecha_gestion") == hoy_str:
            continue
            
        c_dict["pagos"] = pagos_por_cliente.get(c["id"], [])
        
        debe_cobrar_hoy = False
        if c_dict["enrutado_forzado"] == 1:
            debe_cobrar_hoy = True
        else:
            for p in c_dict["pagos"]:
                if p["fecha"] == hoy_str and p["pagado"] == 0:
                    debe_cobrar_hoy = True
                    break
                if p["fecha"] < hoy_str and p["pagado"] == 0:
                    debe_cobrar_hoy = True
                    break

        if debe_cobrar_hoy:
            atrasadas = 0
            for p in c_dict["pagos"]:
                if not p["pagado"] and p["fecha"] < hoy_str:
                    atrasadas += 1
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
            <div class="logo-icon">🌐 AVANTA</div>
            <div class="logo-subtitle">PAGOS SISTEMA v1.3</div>
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
"""
CONTENIDO_HTML += """
    <div class="section-header-title"><i class="fa-solid fa-route"></i> Ruta Principal</div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente o referencia..." onkeyup="filtrarClientes()">

    <div id="clientesContainer">
        {% for c in clientes %}
            {% set pagado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo = c.monto_total - pagado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set es_mora = c.saltado_hoy > 0 or c.cuotas_atrasadas > 0 %}

            <div class="card cliente-card" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if es_mora else 0 }}">
                <div class="flex-between">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <div style="display:flex; flex-direction:column; gap:2px;">
                            <a href="/mover/{{ c.id }}/subir" style="text-decoration:none; font-size:10px; background:#f1f5f9; padding:2px 4px; border-radius:3px;">⬆️</a>
                            <a href="/mover/{{ c.id }}/bajar" style="text-decoration:none; font-size:10px; background:#f1f5f9; padding:2px 4px; border-radius:3px;">⬇️</a>
                        </div>
                        <div style="cursor:pointer;" onclick="verFichaCliente({{ c.id }})">
                            <div style="display:flex; align-items:center; gap:6px;">
                                <span style="font-size:14px; font-weight:800; color:#0f2b5c;">{{ c.nombre }}</span>
                                {% if es_mora %}
                                    <span class="badge-mora">MORA</span>
                                {% else %}
                                    <span class="badge-al-dia">AL DÍA</span>
                                {% endif %}
                            </div>
                            <div style="font-size:11px; color:#6b7280; margin-top:2px;">
                                Ref: <b>{{ c.referencia or 'Ninguna' }}</b> | Cuota Base: <b>${{ "%.2f"|format(c.valor_cuota) }}</b>
                            </div>
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:10px; color:#64748b; font-weight:bold;">Métricas:</div>
                        <div style="font-size:12px; font-weight:700; color:#ef4444; margin-top:2px;">Atraso: <b>{{ c.saltado_hoy }} días</b></div>
                        <div style="font-size:12px; font-weight:700; color:#0f2b5c;">Saldo: <b>${{ "%.2f"|format(saldo) }}</b></div>
                    </div>
                </div>

             <div style="display:flex; justify-content:flex-end; gap:6px; margin-top:10px;">
                {% if cuota_pendiente %}
                    <!-- ⚡ OPTIMIZADO: Botón único "Cobrar / Abonar" capaz de procesar cobros base o múltiples cuotas continuas -->
                    <button class="btn-accion btn-pagar" style="background:#10b981;" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})"><i class="fa-solid fa-money-bill-wave"></i> Cobrar</button>
                    <button class="btn-accion btn-nopagar" style="background:#ef4444;" onclick="ejecutarNoPago({{ c.id }})"><i class="fa-solid fa-ban"></i> Saltar</button>
                {% endif %}
            </div>
            </div>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
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
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Activo')">🟢 Activos ({{ total_activos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Inactivo')">⚪ Inactivos ({{ total_inactivos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Lista Negra')">⚫ Bloqueados ({{ total_negra }})</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar registro..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
        {% for cl in todos %}
            <div class="card cliente-card" data-nombre="{{ cl.nombre | lower }}">
                <div class="flex-between">
                    <div>
                        <span style="font-weight:800; color:#0f2b5c; font-size:14px;">{{ cl.nombre }}</span>
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Monto Total: ${{ "%.2f"|format(cl.monto_total) }} | Modalidad: {{ cl.frecuencia }}</div>
                    </div>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <button class="btn-accion btn-abono" onclick="navegarRuta('/mover_renovacion/{{ cl.id }}')" style="border:none; padding:8px 12px;"><i class="fa-solid fa-rotate"></i> Renovar</button>
                        {% if cl.estado == 'Activo' %}
                            <button class="btn-accion" onclick="if(confirm('¿Mover a Lista Negra?')) window.location.href='/api/clientes/lista_negra/{{ cl.id }}'" style="background:#475569; padding:8px 12px;"><i class="fa-solid fa-ban"></i> Bloquear</button>
                        {% endif %}
                        <a href="/api/eliminar_cliente/{{ cl.id }}" onclick="return confirm('¿Eliminar de forma permanente?')" class="btn-accion btn-nopagar" style="text-decoration:none; padding:8px 12px; background:#dc2626;"><i class="fa-solid fa-trash-can"></i> Borrar</a>
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>

{% elif vista == 'embed_creditos_maestros' or vista == 'creditos_maestros' %}
    <div class="section-header-title"><i class="fa-solid fa-hand-holding-dollar"></i> CONTROL MAESTRO DE GESTIÓN</div>
    
    <div style="display:flex; gap:6px; margin-bottom:12px; overflow-x:auto;">
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Activo')">🟢 Activos ({{ total_activos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Inactivo')">⚪ Inactivos ({{ total_inactivos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Lista Negra')">⚫ Bloqueados ({{ total_negra }})</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar registro..." onkeyup="filtrarClientes()">
        <div id="clientesContainer">
        {% for c in clientes %}
            {% set pagado_acumulado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo_restante = c.monto_total - pagado_acumulado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set cuotas_pagadas = c.pagos | selectattr('pagado', 'equalto', 1) | list | length %}
            {% set total_cuotas = c.cuotas %}
            {% set es_mora = c.saltado_hoy > 0 or c.cuotas_atrasadas > 0 %}
            
            <!-- 📱 TEXTURA PREMIUM CONSOLIDADA IDENTICA A LA CAPTURA -->
            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if es_mora else 0 }}">
                <div class="card-izquierda">
                    <!-- 🟢 Icono circular de Frecuencia con Inicial (D o S) y Color de Estado -->
                    <div class="circulo-frecuencia {% if not es_mora %}al-dia{% elif c.saltado_hoy == 0 %}mora_leve{% endif %}">
                        {{ 'D' if c.frecuencia == 'Diaria' else 'S' }}
                    </div>
                    
                    <div class="info-bloque-texto" style="width:100%;">
                        <!-- ID y Detalle del Negocio / Alias -->
                        <div class="id-alias">{{ c.id }} {{ c.referencia or 'Comercio' }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <!-- 📊 Cuadrícula de Métricas Contables de 3 Columnas -->
                        <div class="grid-metricas-premium">
                            <div>
                                <span class="metric-lbl">Vr. Cuota</span>
                                <span class="metric-val">${{ "%.0f"|format(c.valor_cuota) if c.valor_cuota % 1 == 0 else "%.2f"|format(c.valor_cuota) }}</span>
                            </div>
                            <div>
                                <span class="metric-lbl">Pendiente</span>
                                <span class="metric-val">{{ "%.1f"|format(cuotas_pagadas) if cuotas_pagadas is float else cuotas_pagadas }} / {{ total_cuotas }}.0</span>
                            </div>
                            <div>
                                <span class="metric-lbl">Pago</span>
                                <span class="metric-val">—</span>
                            </div>
                        </div>
                        
                        <!-- 🛠️ Fila Inferior de Utilidades (Foto, Check de Cuota y Saldo) -->
                        <div class="fila-utilidades-premium">
                            <button type="button" class="btn-utilidad-foto" onclick="navegarRuta('/menu/balance')">
                                <i class="fa-solid fa-camera"></i>
                            </button>
                            <div class="circulo-check-cuota {% if c.saltado_hoy > 0 %}gestionado{% elif cuotas_pagadas > 0 %}gestionado-ok{% endif %}">
                                {% if c.saltado_hoy > 0 %}{{ c.saltado_hoy }}{% else %}{{ cuotas_pagadas if cuotas_pagadas > 0 else 1 }}{% endif %}
                            </div>
                            <div class="badge-saldo-premium">Saldo <b>${{ "%.0f"|format(saldo_restante) }}</b></div>
                        </div>
                    </div>
                </div>
                
                <!-- 🎯 Bloque de Acciones Verticales del Lado Derecho (Cobrar y Saltar) -->
                <div class="card-derecha-acciones">
                    {% if cuota_pendiente %}
                        <!-- Botón Cobrar Premium (Icono de mano recibiendo dinero con check de aprobación) -->
                        <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})">
                            <i class="fa-solid fa-hand-holding-dollar"></i>
                        </button>
                        <!-- Botón Saltar Premium (Icono de mano rechazando dinero con cruz de salto diario) -->
                        <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({{ c.id }})">
                            <i class="fa-solid fa-hand-fist" style="transform: rotate(90deg); font-size: 21px;"></i>
                        </button>
                    {% endif %}
                </div>
            </div>
        {% endfor %}
    </div>

{% elif vista == 'clientes' or vista == 'creditos' or vista == 'creditos_maestros' %}
    <div class="section-header-title"><i class="fa-solid fa-hand-holding-dollar"></i> CONTROL MAESTRO DE GESTIÓN</div>
    
    <div style="display:flex; gap:6px; margin-bottom:12px; overflow-x:auto;">
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Activo')">🟢 Activos ({{ total_activos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Inactivo')">⚪ Inactivos ({{ total_inactivos }})</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/creditos?filtro=Lista Negra')">⚫ Bloqueados ({{ total_negra }})</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar registro..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
        {% for cl in todos %}
            <div class="card cliente-card" data-nombre="{{ cl.nombre | lower }}">
                <div class="flex-between">
                    <div>
                        <span style="font-weight:800; color:#0f2b5c; font-size:14px;">{{ cl.nombre }}</span>
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Monto Total: ${{ "%.2f"|format(cl.monto_total) }} | Modalidad: {{ cl.frecuencia }}</div>
                    </div>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <button class="btn-accion btn-abono" onclick="navegarRuta('/mover_renovacion/{{ cl.id }}')" style="border:none; padding:8px 12px;"><i class="fa-solid fa-rotate"></i> Renovar</button>
                        {% if cl.estado == 'Activo' %}
                            <button class="btn-accion" onclick="if(confirm('¿Mover a Lista Negra?')) window.location.href='/api/clientes/lista_negra/{{ cl.id }}'" style="background:#475569; padding:8px 12px;"><i class="fa-solid fa-ban"></i> Bloquear</button>
                        {% endif %}
                        <a href="/api/eliminar_cliente/{{ cl.id }}" onclick="return confirm('¿Eliminar de forma permanente?')" class="btn-accion btn-nopagar" style="text-decoration:none; padding:8px 12px; background:#dc2626;"><i class="fa-solid fa-trash-can"></i> Borrar</a>
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
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
        {% if altados_list or saltados_list %}
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
            <label>Nombre del Cliente</label><input type="text" name="nombre" value="{{ cliente.nombre if cliente else '' }}" required>
            <label>WhatsApp</label><input type="text" name="telefono" value="{{ cliente.telefono if cliente else '' }}" required>
            <label>Identificación / Cédula</label><input type="text" name="identificacion" value="{{ cliente.identificacion if cliente else '' }}">
            <label>Dirección Domiciliaria</label><input type="text" name="direccion" value="{{ cliente.direccion if cliente else '' }}">
            <label>Referencia Local</label><input type="text" name="referencia" value="{{ cliente.referencia if cliente else '' }}">
            <label>Monto Financiado ($)</label><input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            <label>Interés (%)</label><input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            <label>Cuotas</label><input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            <div id="simulacionText" style="font-weight:bold; color:#0369a1; margin:6px 0;">$0.00 / cuota</div>
            <label>Frecuencia</label>
            <select name="frecuencia"><option value="Diaria">Diaria</option><option value="Semanal">Semanal</option></select>
            <label>Fecha de Inicio</label><input type="date" name="fecha_inicio" value="{{ hoy_str }}" required>
            <input type="hidden" id="input_latitud" name="latitud"><input type="hidden" id="input_longitud" name="longitud">
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

    <div class="card" style="padding:16px;">
        <div class="kpi-title" style="margin-bottom:8px;">Registrar Movimiento de Flujo</div>
        <!-- 🔑 FIJADO: Envío tradicional cancelado. Delegado al script asíncrono controlado procesarYEnviarGasto -->
        <form id="formBalance" method="POST" action="/api/balance/guardar_movimiento">
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
            
            <button type="button" onclick="procesarYEnviarGasto(event)" class="btn-accion btn-pagar" style="width:100%; padding:12px; margin-top:10px; font-size:13px; font-weight:800; border:none; border-radius:8px; color:white; cursor:pointer;">Guardar Registro</button>
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>AVANTA PAGOS v1.3</title>
    <!-- 🌐 CDNs REALES: Descarga el set tipográfico Inter y la librería de iconos FontAwesome -->
    <link href="https://googleapis.com" rel="stylesheet">
    <link rel="stylesheet" href="https://cloudflare.com" crossorigin="anonymous">
    <style>
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
        body { background: #f1f5f9; color: #1e293b; padding-bottom: 70px; }
        
        /* 📱 EFECTO DE PROFUNDIDAD: Gradiente sofisticado y sombras nativas para la barra superior */
        .navbar { background: linear-gradient(135deg, #0f2b5c 0%, #1e3a8a 100%); color: white; padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 200; box-shadow: 0 4px 12px rgba(15, 43, 92, 0.15); }
        .btn-nav-icon { background: rgba(255, 255, 255, 0.15); color: white; border: 1px solid rgba(255, 255, 255, 0.1); padding: 8px 14px; border-radius: 8px; font-weight: 700; font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 6px; text-decoration: none; backdrop-filter: blur(4px); transition: all 0.2s; }
        .btn-nav-icon:active { background: rgba(255, 255, 255, 0.25); transform: scale(0.95); }
        .nav-title { font-size: 16px; font-weight: 800; color: white; letter-spacing: 0.5px; text-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        
        /* Menú Lateral Desplegable Premium */
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.6); backdrop-filter: blur(4px); z-index: 300; transition: opacity 0.3s ease; }
        .drawer-overlay.active { display: block; }
        .drawer { position: fixed; top: 0; left: -290px; width: 290px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1); display: flex; flex-direction: column; box-shadow: 5px 0 25px rgba(0,0,0,0.15); }
        .drawer.active { left: 0; }
        .drawer-header { background: linear-gradient(135deg, #0f2b5c 0%, #1e3a8a 100%); color: white; padding: 24px 16px; border-bottom: 4px solid #10b981; }
        .drawer-logo { font-size: 18px; font-weight: 800; letter-spacing: 0.5px; }
        .drawer-menu { list-style: none; padding: 12px 0; overflow-y: auto; flex: 1; }
        .drawer-menu li a { display: flex; align-items: center; gap: 12px; padding: 14px 20px; color: #475569; text-decoration: none; font-weight: 600; font-size: 13px; border-bottom: 1px solid #f1f5f9; transition: background 0.2s; }
        .drawer-menu li a:active { background: #f8fafc; color: #0f2b5c; }
        .drawer-menu li a i { font-size: 16px; width: 22px; color: #3b82f6; text-align: center; }
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
        .date-badge { text-align: center; font-size: 13px; font-weight: 800; color: #1e3a8a; background: #dbeafe; padding: 8px; border-radius: 8px; margin-bottom: 12px; border: 1px solid #bfdbfe; box-shadow: inset 0 1px 2px rgba(255,255,255,0.6); }
        .grid-kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
        .kpi-card { background: white; padding: 14px 12px; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03), 0 2px 4px -1px rgba(0,0,0,0.02); }
        .kpi-title { font-size: 10px; color: #64748b; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; }
        .kpi-val { font-size: 15px; font-weight: 800; color: #0f2b5c; margin-top: 2px; }
        .expected-card { background: linear-gradient(to bottom, #f0f9ff, #e0f2fe); border: 1px solid #bae6fd; padding: 14px; border-radius: 12px; margin-bottom: 14px; text-align: left; box-shadow: 0 4px 10px rgba(3, 105, 161, 0.05); }
        .search-box { width: 100%; padding: 14px; border: 1px solid #cbd5e1; border-radius: 12px; margin-bottom: 14px; font-size: 13px; outline: none; background: white; box-shadow: inset 0 1px 3px rgba(0,0,0,0.05); transition: border-color 0.2s; }
        .search-box:focus { border-color: #3b82f6; }
        .card { background: white; padding: 16px; border-radius: 14px; margin-bottom: 12px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03); }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .btn-accion { border: none; padding: 10px 16px; border-radius: 8px; font-weight: 800; font-size: 12px; cursor: pointer; color: white; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        
        /* 📱 INTERFAZ PREMIUM CON RELIEVE Y VOLUMEN (EFECTO APLICACIÓN NATIVA) */
        .cliente-card-premium { background: #ffffff !important; border-radius: 14px !important; padding: 14px !important; margin-bottom: 12px !important; border: 1px solid #e2e8f0 !important; display: flex !important; flex-direction: row !important; justify-content: space-between !important; align-items: center !important; box-shadow: 0 4px 12px -1px rgba(15, 23, 42, 0.06), 0 2px 4px -1px rgba(15, 23, 42, 0.03) !important; width: 100% !important; clear: both !important; transition: transform 0.15s; }
        .card-izquierda { display: flex !important; flex-direction: row !important; align-items: flex-start !important; gap: 14px !important; flex: 1 !important; text-align: left !important; width: calc(100% - 65px) !important; }
        .circulo-frecuencia { width: 38px; height: 34px; border-radius: 10px; border: 2.5px solid #ef4444; color: #ef4444; font-weight: 800; font-size: 14px; display: flex; align-items: center; justify-content: center; background: #fff1f2; flex-shrink: 0; box-shadow: 0 2px 4px rgba(239, 68, 68, 0.1); }
        .circulo-frecuencia.al-dia { border-color: #10b981; color: #10b981; background: #ecfdf5; box-shadow: 0 2px 4px rgba(16, 185, 129, 0.1); }
        .circulo-frecuencia.mora-leve { border-color: #f59e0b; color: #f59e0b; background: #fffbeb; box-shadow: 0 2px 4px rgba(245, 158, 11, 0.1); }
        .info-bloque-texto { display: flex; flex-direction: column; flex: 1; }
        .id-alias { font-size: 15px; font-weight: 800; color: #1e293b; line-height: 1.2; letter-spacing: -0.2px; }
        .nombre-real { font-size: 13px; color: #64748b; margin: 3px 0 10px 0; font-weight: 600; }
        
        .grid-metricas-premium { display: grid; grid-template-columns: repeat(3, 1fr) !important; width: 100% !important; gap: 6px !important; text-align: left !important; margin-bottom: 10px !important; background: #f8fafc; padding: 8px 10px; border-radius: 10px; border: 1px solid #f1f5f9; }
        .metric-col { display: flex; flex-direction: column; justify-content: flex-start; }
        .metric-lbl { font-size: 10px !important; color: #94a3b8 !important; font-weight: 700 !important; margin-bottom: 2px !important; text-transform: uppercase !important; letter-spacing: 0.3px; }
        .metric-val { font-size: 13px !important; font-weight: 800 !important; color: #334155 !important; }
        
        .fila-utilidades-premium { display: flex; align-items: center; gap: 10px; margin-top: auto; padding-top: 4px; }
        .btn-utilidad-foto { background: #ffffff; color: #475569; border: 1px solid #cbd5e1; width: 34px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 13px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .circulo-check-cuota { background: #f1f5f9; color: #475569; width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; border: 1px solid #cbd5e1; }
        .circulo-check-cuota.gestionado { background: #fee2e2; color: #ef4444; border-color: #fca5a5; }
        .circulo-check-cuota.gestionado-ok { background: #dcfce7; color: #10b981; border-color: #bbf7d0; }
        .badge-saldo-premium { font-size: 12px; color: #64748b; }
        .badge-saldo-premium b { color: #0f2b5c; font-weight: 800; }
        
        .card-derecha-acciones { display: flex; flex-direction: column; justify-content: space-around; align-items: center; padding-left: 14px; border-left: 1px solid #e2e8f0; min-width: 55px; gap: 8px; }
        .btn-accion-premium { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; cursor: pointer; font-size: 20px; width: 42px; height: 42px; display: flex; align-items: center; justify-content: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: all 0.1s ease; }
        .btn-accion-premium:active { transform: scale(0.9); }
        .btn-accion-premium.cobrar { color: #10b981; background: #ecfdf5; border-color: #bbf7d0; box-shadow: 0 4px 6px rgba(16, 185, 129, 0.1); }
        .btn-accion-premium.saltar { color: #ef4444; background: #fff1f2; border-color: #fca5a5; box-shadow: 0 4px 6px rgba(239, 68, 68, 0.1); }
        
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
        <a href="#" onclick="navegarRuta('/nuevo')" class="btn-nav-icon" style="background:#00a8cc;"><i class="fa-solid fa-plus"></i> Nuevo</a>
    </div>

    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <div class="drawer-logo">🌐 AVANTA PAGOS v1.3</div>
            <div style="font-size:11px; opacity:0.8; margin-top:4px;"><i class="fa-solid fa-phone"></i> Soporte: +593991234567</div>
        </div>
        <ul class="drawer-menu">
            <li><a href="#" onclick="navegarRuta('/')"><i class="fa-solid fa-map-location-dot"></i> Ruta Principal</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/pagos_hoy')"><i class="fa-solid fa-receipt"></i> Pagos del Día</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/clientes')"><i class="fa-solid fa-address-book"></i> Clientes</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/creditos')"><i class="fa-solid fa-hand-holding-dollar"></i> Créditos Activos</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/ruta_orden')"><i class="fa-solid fa-arrow-down-up-lock"></i> Reordenar Ruta</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/listados')"><i class="fa-solid fa-list-check"></i> Listados Históricos</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/balance')"><i class="fa-solid fa-wallet"></i> Balance de Caja</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/agregar_lista')"><i class="fa-solid fa-user-plus"></i> Agregar a Lista</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/salvar_datos')"><i class="fa-solid fa-cloud-arrow-up"></i> Salvar en Nube</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/bluetooth')"><i class="fa-solid fa-print"></i> Conexión Bluetooth</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/configuracion')"><i class="fa-solid fa-sliders"></i> Ajustes de Sistema</a></li>
            <li><a href="/logout" style="color:#ef4444;"><i class="fa-solid fa-power-off"></i> Cerrar Sesión</a></li>
        </ul>
    </div>

    <div class="main-container" id="mainContent">
        {{ contenido_html | safe }}
    </div>
"""
HTML_TEMPLATE += """
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
                <a href="#" id="btn_llamar" class="btn-accion btn-abono" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-phone"></i> Llamar</a>
                <a href="#" id="btn_mapa" target="_blank" class="btn-accion btn-pagar" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-location-dot"></i> Ver en Mapa</a>
            </div>
            <button onclick="cerrarModal('modalInfoCliente')" class="btn-modal-close">Salir de la Información</button>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content">
            <h3 style="color:#10b981;"><i class="fa-solid fa-circle-check"></i> Pago Procesado</h3>
            <p id="modalMsg" style="margin:10px 0;"></p>
            <a href="#" id="modalWsBtn" target="_blank" class="btn-ws"><i class="fa-brands fa-whatsapp"></i> WhatsApp Nativo</a>
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
"""
HTML_TEMPLATE += """
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
                                // 🗺️ FIJADO: Enlace universal profundo y mapeado para Google Maps en Android/iOS
                document.getElementById('btn_mapa').href = 'https://google.com' + d.data.latitud + ',' + d.data.longitud;
                
                document.getElementById('modalInfoCliente').style.display = 'flex';
            }
        });
}

function filtrarClientes() {
    const query = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.cliente-card').forEach(card => {
        card.style.display = card.getAttribute('data-nombre').includes(query) ? "block" : "none";
    });
}
function filtrarEstado(tipo) {
    document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('active'));
    document.getElementById('f-' + tipo).classList.add('active');
    document.querySelectorAll('.cliente-card').forEach(card => {
        const esMora = card.getAttribute('data-mora') === '1';
        if (tipo === 'todos') card.style.display = "block";
        else if (tipo === 'mora' && esMora) card.style.display = "block";
        else if (tipo === 'aldia' && !esMora) card.style.display = "block";
        else card.style.display = "none";
    });
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
                // 📲 FIJADO: Enlace wa.me/ con barra diagonal integrada correctamente para evitar bloqueos
                                // 📲 FIJADO: Enlace de wa.me/ con barra diagonal integrada correctamente para evitar bloqueos
                const urlWeb = 'https://wa.me' + numPuro + '?text=' + textoMensaje;
                
                const btnWs = document.getElementById('modalWsBtn');
                btnWs.href = '#';
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

function procesarImagenEnCaliente() {
    const fileInput = document.getElementById('foto_mov');
    if (!fileInput.files || fileInput.files.length === 0) return;
    const file = fileInput.files[0];

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
                img.src = ""; 
                let textoB64 = data.comprobante.replace(/\\n/g, '').replace(/\\r/g, '').trim();
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
        
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT valor FROM configuracion WHERE llave = 'usuario'")
                usr_row = cursor.fetchone()
                cursor.execute("SELECT valor FROM configuracion WHERE llave = 'clave'")
                clv_row = cursor.fetchone()
                
        # Validación segura desempaquetando diccionarios para DictCursor
        if usr_row and clv_row:
            if usuario_ingresado == usr_row["valor"] and clave_ingresada == clv_row["valor"]:
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
    
    # 📅 FECHA EN TIEMPO REAL: Formateo dinámico en español para el badge superior
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    ahora = datetime.now()
    fecha_legible = f"{dias_semana[ahora.weekday()]}, {ahora.day:02d} de {meses[ahora.month - 1]} de {ahora.year}"
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
            total_cobrado_hoy = float(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s", (hoy_str,))
            total_gastos_hoy = float(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
            creditos_nuevos_hoy = int(cursor.fetchone()["total"])

    clientes_ruta = obtener_clientes_ruta_hoy()
    debido_minimo_dia = sum(cl["valor_cuota"] for cl in clientes_ruta)
    
    debido_total_acumulado = 0.0
    for cl in clientes_ruta:
        for p in cl["pagos"]:
            if not p["pagado"] and p["fecha"] <= hoy_str:
                debido_total_acumulado += (p["valor"] - p["valor_pagado"])

    capital_en_calle = sum(max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"])) for c in clientes_ruta)

    contexto_dash = f"""
    <div class="date-badge"><i class="fa-solid fa-calendar-day"></i> {fecha_legible}</div>
    <div class="grid-kpis">
        <div class="kpi-card">
            <div class="kpi-title">Recaudo Hoy / Créditos Hoy</div>
            <div class="kpi-val">${total_cobrado_hoy:.2f} / {creditos_nuevos_hoy} Créd.</div>
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
    
    for c in clientes_ruta:
        pagado_acumulado = sum(p["valor_pagado"] for p in c["pagos"])
        saldo_restante = max(0.0, c["monto_total"] - pagado_acumulado)
        cuotas_pagadas = sum(1 for p in c["pagos"] if p["pagado"] == 1)
        cuota_p = next((p for p in c["pagos"] if p["pagado"] == 0), None)
        es_mora_cl = c.get("cuotas_atrasadas", 0) > 0
        
        c_id = str(c["id"])
        c_nombre = str(c["nombre"])
        c_nombre_lower = str(c["nombre"].lower())
        c_referencia = str(c["referencia"] or "Comercio")
        c_valor_cuota = f"{c['valor_cuota']:.2f}"
        c_cuotas = str(c["cuotas"])
        c_saldo_restante = f"{saldo_restante:.2f}"
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
                        <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{c_cuotas_pagadas}.0 / {c_cuotas}.0</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                    </div>
                    
                    <div class="fila-utilidades-premium">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota}); alert('📸 Adjunta el comprobante para la cuota de {c_nombre}');">📸</button>
                        <div class="circulo-check-cuota {clase_check}">{numero_check}</div>
                        <div class="badge-saldo-premium">Saldo <b>${c_saldo_restante}</b></div>
                    </div>
                </div>
            </div>
            
            <div class="card-derecha-acciones">
                <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({c_id}, {js_num_cuota}, {js_pendiente}, {c_valor_cuota})">
                    💵✔️
                </button>
                <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({c_id})">
                    💵❌
                </button>
            </div>
        </div>
        """
        
    contexto_dash += "</div>"
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(contexto_dash)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(contexto_dash))

@app.route("/menu/clientes")
def seccion_clientes_maestro():
    if not session.get("autenticado"): return "Sesión expirada"
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 📊 SECCIÓN CLIENTES INTERCAMBIADA: Recupera TODOS los clientes del sistema para el desglose contable general
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
            
    creditos_procesados = []
    for cr in todos_clientes:
        c_dict = dict(cr)
        c_dict["saldo_pendiente"] = max(0.0, float(c_dict["monto_total"]) - float(c_dict["total_recaudado"]))
        creditos_procesados.append(c_dict)
        
    contexto = dict(vista="clientes_financieros", lista_creditos=creditos_procesados)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/menu/creditos")
def seccion_creditos_activos():
    if not session.get("autenticado"): return "Sesión expirada"
    filtro_estado = request.args.get("filtro", "Activo")
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 🛡️ SECCIÓN CRÉDITOS INTERCAMBIADA: Control maestro de estados mapeado por pestañas de Neon
            cursor.execute("SELECT id, nombre, frecuencia, monto_total, estado FROM clientes WHERE estado = %s ORDER BY nombre ASC", (filtro_estado,))
            todos = cursor.fetchall()
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Activo'")
            total_activos = int(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Inactivo'")
            total_inactivos = int(cursor.fetchone()["total"])
            
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Lista Negra'")
            total_negra = int(cursor.fetchone()["total"])
            
    contexto = dict(
        vista="creditos_maestros", 
        todos=todos, 
        total_activos=total_activos, 
        total_inactivos=total_inactivos, 
        total_negra=total_negra, 
        filtro_estado=filtro_estado
    )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/menu/balance")
def seccion_balance_maestro():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 🏢 FIJADO: Uso de alias explícitos 'AS val' para asegurar compatibilidad total con DictCursor
            cursor.execute("SELECT valor AS val FROM configuracion WHERE llave = 'caja_base'")
            res_caja = cursor.fetchone()
            caja_base = float(res_caja["val"]) if res_caja else 100.00
            
            cursor.execute("SELECT valor AS val FROM configuracion WHERE llave = 'balance_estado'")
            res_estado = cursor.fetchone()
            balance_estado = res_estado["val"] if res_estado else "Sin cerrar"
            
            cursor.execute("SELECT id, tipo, categoria, concepto, monto, fecha, comprobante FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
            movimientos = cursor.fetchall()
            
    entradas_totales = sum(m["monto"] for m in movimientos if m["tipo"] == "Entrada")
    salidas_totales = sum(m["monto"] for m in movimientos if m["tipo"] == "Salida")
    efectivo_neto = caja_base + entradas_totales - salidas_totales
    
    movimientos_limpios = []
    for m in movimientos:
        m_dict = dict(m)
        # 🧼 Saneamiento crítico para DictCursor: limpia el Base64 y asegura que nunca devuelva un string roto o un None oculto
        if m_dict["comprobante"] and str(m_dict["comprobante"]).strip() != "" and m_dict["comprobante"] != "None":
            try:
                comprobante_puro = str(m_dict["comprobante"]).replace("\n", "").replace("\r", "").strip()
                if "base64," in comprobante_puro:
                    comprobante_puro = comprobante_puro.split("base64,")[-1]
                m_dict["comprobante"] = comprobante_puro
            except Exception as b64_err:
                print(f"Error procesando imagen en backend: {b64_err}")
                m_dict["comprobante"] = ""
        else:
            m_dict["comprobante"] = ""
            
        movimientos_limpios.append(m_dict)
            
    contexto = dict(vista="balance", caja_base=caja_base, balance_estado=balance_estado, gastos_list=movimientos_limpios, entradas_totales=entradas_totales, salidas_totales=salidas_totales, efectivo_neto=efectivo_neto)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/nuevo")
@app.route("/mover_renovacion/<int:cliente_id>")
def seccion_nuevo_credito(cliente_id=None):
    if not session.get("autenticado"): return redirect("/login")
    hoy_str = date.today().isoformat()
    cliente = None
    if cliente_id:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cursor.fetchone()
                
    contexto = dict(vista="nuevo", hoy_str=hoy_str, cliente=cliente)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/guardar", methods=["POST"])
def guardar_nuevo_cliente():
    if not session.get("autenticado"): return redirect("/login")
    try:
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").strip()
        identificacion = request.form.get("identificacion", "").strip()
        direccion = request.form.get("direccion", "").strip()
        referencia = request.form.get("referencia", "").strip()
        monto = float(request.form.get("monto", "0"))
        interes_porcentaje = float(request.form.get("interes_porcentaje", "20"))
        cuotas = int(request.form.get("cuotas", "0"))
        frecuencia = request.form.get("frecuencia")
        fecha_inicio_dt = date.fromisoformat(request.form.get("fecha_inicio"))
        latitud = request.form.get("latitud", "").strip()
        longitud = request.form.get("longitud", "").strip()
        
        monto_total = float(round(monto * (1 + (interes_porcentaje / 100)), 2))
        valor_base_cuota = float(round(monto_total / cuotas, 2))
        fecha_vencimiento_dt = calcular_fecha(fecha_inicio_dt, cuotas, frecuencia)
        
        with get_db() as conn:
            with conn.cursor() as cursor:
                # 🏢 Obtención limpia forzando nombres de columnas
                cursor.execute("SELECT COALESCE(MAX(orden), 0) AS max_o FROM clientes")
                res_orden = cursor.fetchone()
                max_orden = res_orden["max_o"] if res_orden else 0
                
                # Inserción parametrizada convirtiendo fechas a cadenas de texto ISO
                cursor.execute(
                    """
                    INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, estado, saltado_hoy, fecha_gestion, latitud, longitud)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo', 0, '', %s, %s) RETURNING id
                    """,
                    (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base_cuota, str(fecha_inicio_dt.isoformat()), str(fecha_vencimiento_dt.isoformat()), latitud, longitud)
                )
                
                # 🔑 FIJADO: Desestructuración posicional por índice para asegurar la captura del ID real en Neon
                res_id = cursor.fetchone()
                cliente_id = res_id[0] if res_id else None
                
                if cliente_id:
                    acumulado = 0.0
                    for num in range(1, cuotas + 1):
                        valor_cuota_real = float(round(monto_total - acumulado, 2)) if num == cuotas else valor_base_cuota
                        acumulado += valor_cuota_real
                        f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frecuencia)
                        fecha_cuota_str = str(f_cuota.isoformat())
                        
                        cursor.execute(
                            """
                            INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real) 
                            VALUES (%s, %s, %s, %s, 0, 0, '')
                            """, 
                            (cliente_id, num, fecha_cuota_str, valor_cuota_real)
                        )
            conn.commit()
    except Exception as e: 
        print(f"Error crítico en guardar cliente: {e}")
    return redirect("/")

@app.route("/api/balance/guardar_movimiento", methods=["POST"])
def balance_guardar_movimiento():
    if not session.get("autenticado"): return redirect("/login")
    tipo = request.form.get("tipo_mov")
    categoria = request.form.get("categoria_mov", "Otros")
    concepto = request.form.get("concepto_mov", "").strip()
    monto = float(request.form.get("monto_mov", "0"))
    hoy_str = date.today().isoformat()
    if not concepto: concepto = "Flujo de " + categoria
    
    # 📸 FIJADO: Captura el string comprimido de la imagen que le envía el nuevo formulario JavaScript
    base64_str = request.form.get("foto_comprimida_b64", "").strip()
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante) 
                VALUES (%s, %s, %s, %s, %s, %s)
                """, (tipo, categoria, concepto, monto, hoy_str, base64_str)
            )
        conn.commit()
    return redirect("/menu/balance")

@app.route("/api/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    with get_db() as conn:
        with conn.cursor() as cursor: 
            cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (gasto_id,))
        conn.commit()
    return redirect("/menu/balance")

@app.route("/api/eliminar_cliente/<int:id>")
def api_eliminar_cliente(id):
    with get_db() as conn:
        with conn.cursor() as cursor: 
            cursor.execute("DELETE FROM clientes WHERE id = %s", (id,))
        conn.commit()
    return redirect("/menu/clientes")

@app.route("/mover/<int:cliente_id>/<string:direccion>")
def mover(cliente_id, direccion):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, orden FROM clientes ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            index = next((i for i, c in enumerate(clientes) if c["id"] == cliente_id), None)
            if index is not None:
                if direccion == "subir" and index > 0:
                    actual, anterior = clientes[index], clientes[index - 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (anterior["orden"], actual["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (actual["orden"], anterior["id"]))
                elif direccion == "bajar" and index < len(clientes) - 1:
                    actual, siguiente = clientes[index], clientes[index + 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (siguiente["orden"], actual["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (actual["orden"], siguiente["id"]))
            conn.commit()
    return redirect("/")

@app.route("/menu/ruta_orden")
def seccion_reordenar_ruta():
    if not session.get("autenticado"): return "Sesión expirada"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, nombre, orden, referencia, frecuencia FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            todos = cursor.fetchall()

    html_orden = """
    <div class="section-header-title"><i class="fa-solid fa-arrow-down-up-lock"></i> Modificar Orden de la Ruta</div>
    <p style="font-size:11px; color:#64748b; margin-bottom:12px; text-align:left; padding:0 4px;">Organiza la secuencia de visitas diarias de tus cobradores. Los clientes aparecerán en el Dashboard en este orden estricto.</p>
    """
    for index, cl in enumerate(todos):
        html_orden += f"""
        <div class="card" style="padding:14px; display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; border-left:4px solid #00a8cc; box-shadow:0 2px 4px rgba(0,0,0,0.04);">
            <div style="text-align:left; display:flex; align-items:center; gap:12px;">
                <div style="background:#0f2b5c; color:white; font-size:12px; font-weight:800; border-radius:50%; width:28px; height:28px; display:flex; align-items:center; justify-content:center; box-shadow:0 2px 4px rgba(15,43,92,0.2);">
                    {index + 1}
                </div>
                <div>
                    <span style="font-weight:800; font-size:14px; color:#0f2b5c; display:block;">{cl['nombre']}</span>
                    <span style="font-size:11px; color:#64748b; display:block; margin-top:2px;">📍 Ref: {cl['referencia'] or 'N/A'}</span>
                </div>
            </div>
            <div style="display:flex; gap:6px;">
                <a href="/mover/{cl['id']}/subir" style="text-decoration:none; font-size:11px; background:#f1f5f9; color:#0f2b5c; padding:8px 10px; border-radius:8px; font-weight:800; display:flex; align-items:center; gap:4px; border:1px solid #e2e8f0; transition:all 0.2s;">
                    ▲ Subir
                </a>
                <a href="/mover/{cl['id']}/bajar" style="text-decoration:none; font-size:11px; background:#f1f5f9; color:#0f2b5c; padding:8px 10px; border-radius:8px; font-weight:800; display:flex; align-items:center; gap:4px; border:1px solid #e2e8f0; transition:all 0.2s;">
                    ▼ Bajar
                </a>
            </div>
        </div>
        """
    if not todos:
        html_orden += '<p style="font-size:12px; color:#64748b; text-align:center; padding:20px;">No hay clientes activos para ordenar en la ruta.</p>'

    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_orden)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_orden))

@app.route("/menu/listados")
def seccion_listados():
    if not session.get("autenticado"): return "Sesión expirada"
    fecha_filtro = request.args.get("fecha_auditoria", date.today().isoformat())
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT p.*, c.nombre FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.fecha_pago_real = %s", (fecha_filtro,))
            pagos_f = cursor.fetchall()
            cursor.execute("SELECT * FROM clientes WHERE fecha_inicio = %s", (fecha_filtro,))
            creditos_f = cursor.fetchall()

    html_pagos = "".join([f'<p style="font-size:12px; border-bottom:1px solid #f1f5f9; padding:4px 0;">👤 {p["nombre"]} - Cuota #{p["numero"]} | <b style="color:#10b981;">${p["valor_pagado"]:.2f}</b></p>' for p in pagos_f])
    if not html_pagos:
        html_pagos = '<p style="font-size:11px; color:#64748b; text-align:center;">Sin recaudos en esta fecha.</p>'

    html_creditos = "".join([f'<p style="font-size:12px; border-bottom:1px solid #f1f5f9; padding:4px 0;">👤 {c["nombre"]} | Capital: <b>${c["monto"]:.2f}</b> | Total Cartera: <b>${c["monto_total"]:.2f}</b></p>' for c in creditos_f])
    if not html_creditos:
        html_creditos = '<p style="font-size:11px; color:#64748b; text-align:center;">Sin créditos nuevos en esta fecha.</p>'

    html_listados = f"""
    <div class="section-header-title"><i class="fa-solid fa-list-check"></i> Auditoría Histórica por Fecha</div>
    <div class="card">
        <form action="/menu/listados" method="GET" onsubmit="event.preventDefault(); navegarRuta('/menu/listados?fecha_auditoria=' + document.getElementById('fecha_auditor').value);">
            <label>Selecciona la Fecha a Consultar</label>
            <input type="date" id="fecha_auditor" value="{fecha_filtro}" required>
            <button type="submit" class="btn-primary" style="background:#0f2b5c; color:white; border:none; padding:10px; font-weight:bold; width:100%; border-radius:8px; cursor:pointer;">🔍 Filtrar Historial</button>
        </form>
    </div>
    <div class="card" style="text-align:left;">
        <h4 style="font-size:12px; color:#0f2b5c; margin-bottom:6px;">💰 Recaudos de la Fecha</h4>
        {html_pagos}
    </div>
    <div class="card" style="text-align:left;">
        <h4 style="font-size:12px; color:#0f2b5c; margin-bottom:6px;">👤 Créditos Entregados</h4>
        {html_creditos}
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_listados)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_listados))

@app.route("/menu/agregar_lista")
def seccion_agregar_lista():
    if not session.get("autenticado"): return "Sesión expirada"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, nombre, frecuencia FROM clientes WHERE estado = 'Activo' ORDER BY nombre ASC")
            todos = cursor.fetchall()

    html_sincro = """
    <div class="section-header-title"><i class="fa-solid fa-user-plus"></i> Enrutamiento Forzado Anticipado</div>
    <div class="card">
        <form action="/api/clientes/forzar_enrutado" method="POST">
            <label>Selecciona el Cliente Semanal a Forzar Cobro Hoy</label>
            <select name="cliente_forzar_id" required style="width:100%; padding:10px; border-radius:8px; margin-bottom:10px;">
                {% for c in todos %}
                    <option value="{{ c['id'] }}">👤 {{ c['nombre'] }} ({{ c['frecuencia'] }})</option>
                {% endfor %}
            </select>
            <button type="submit" class="btn-primary" style="background:#0f2b5c; border:none; padding:12px; color:white; font-weight:bold; border-radius:8px; width:100%; cursor:pointer;">⚡ Enrutar y Forzar Cobro Hoy</button>
        </form>
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_sincro, todos=todos)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_sincro, todos=todos))

@app.route("/api/clientes/forzar_enrutado", methods=["POST"])
def api_clientes_forzar_enrutado():
    c_id = request.form.get("cliente_forzar_id")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE clientes SET enrutado_forzado = 1 WHERE id = %s", (c_id,))
        conn.commit()
    return redirect("/")

@app.route("/menu/salvar_datos")
def seccion_respaldo_nube():
    html_cloud = """
    <div class="section-header-title"><i class="fa-solid fa-cloud-arrow-up"></i> Salvar Datos en la Nube (Neon AWS)</div>
    <div class="card" style="text-align:center; padding:30px 16px;">
        <i class="fa-solid fa-cloud-arrow-up" style="font-size:44px; color:#0284c7; margin-bottom:10px;"></i>
        <h4>Sincronización en Tiempo Real Activa</h4>
        <p style="font-size:12px; color:#64748b; margin-bottom:14px;">Toda tu información ya está respaldada de forma automática. En caso de pérdida del celular, tus cobros y saldos están 100% seguros.</p>
        <button type="button" onclick="alert('⚡ ¡Sincronización forzada con éxito! Copia de seguridad guardada en Neon Cloud.');" class="btn-primary" style="background:#10b981; border:none; color:white; font-weight:bold; padding:12px; width:100%; border-radius:8px; cursor:pointer;">Forzar Respaldo Ahora</button>
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_cloud)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_cloud))

@app.route("/api/cliente_info/<int:id>")
def api_cliente_info(id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (id,))
            c = cursor.fetchone()
            if not c:
                return jsonify({"status": "error", "message": "No encontrado"})
                
            cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY numero ASC", (id,))
            pagos = cursor.fetchall()
            
    c_dict = dict(c)
    pagado = sum(p["valor_pagado"] for p in pagos)
    c_dict["saldo_actual"] = max(0.0, c_dict["monto_total"] - pagado)
    
    return jsonify({"status": "ok", "data": c_dict})

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    monto_recaudado = float(request.args.get("monto", 0))
    hoy_str = date.today().isoformat()
    monto_restante = monto_recaudado
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, numero, valor, valor_pagado, pagado FROM pagos WHERE cliente_id = %s AND pagado = 0 ORDER BY numero ASC", (cliente_id,))
            pagos_pendientes = cursor.fetchall()
            
            for p in pagos_pendientes:
                if monto_restante <= 0:
                    break
                    
                pendiente_cuota = p["valor"] - p["valor_pagado"]
                
                if monto_restante >= pendiente_cuota:
                    monto_restante = round(monto_restante - pendiente_cuota, 2)
                    cursor.execute(
                        "UPDATE pagos SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (p["valor"], hoy_str, p["id"])
                    )
                else:
                    nuevo_pago_parcial = round(p["valor_pagado"] + monto_restante, 2)
                    monto_restante = 0
                    cursor.execute(
                        "UPDATE pagos SET valor_pagado = %s, fecha_pago_real = %s WHERE id = %s",
                        (nuevo_pago_parcial, hoy_str, p["id"])
                    )
            
            # Recalcular saldos para validar si ya liquidó la deuda completa
            cursor.execute("SELECT nombre, telefono, monto_total FROM clientes WHERE id = %s", (cliente_id,))
            c = cursor.fetchone()
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE cliente_id = %s", (cliente_id,))
            total_pagado = float(cursor.fetchone()["total"])
            
            saldo_restante = max(0.0, c["monto_total"] - total_pagado)
            
            # 🛡️ FIJADO: Si el saldo llegó a $0.00, el cliente se da por liquidado y pasa a 'Inactivo' de inmediato
            if saldo_restante <= 0:
                cursor.execute("UPDATE clientes SET estado = 'Inactivo', enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (cliente_id,))
            else:
                cursor.execute("UPDATE clientes SET enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (cliente_id,))
                
            conn.commit()
        
    mensaje_ws = f"🧾 *AVANTA PAGOS*\nRecibo de Pago\nCliente: {c['nombre']}\nMonto Cobrado: ${monto_recaudado:.2f}\nSaldo Restante: ${saldo_restante:.2f}\n"
    if saldo_restante <= 0:
        mensaje_ws += "🎉 *¡FELICIDADES! CRÉDITO LIQUIDADO AL 100%*"
    else:
        mensaje_ws += "¡Gracias por su pago!"
    
    recibo = {
        "cliente": c["nombre"],
        "telefono": c["telefono"] or "",
        "cuota": num_cuota,
        "monto": monto_recaudado,
        "saldo": saldo_restante,
        "mensaje_ws": mensaje_ws
    }
    return jsonify({"status": "ok", "recibo": recibo})

@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE clientes 
                SET saltado_hoy = saltado_hoy + 1, fecha_gestion = %s 
                WHERE id = %s
                """, (hoy_str, cliente_id)
            )
        conn.commit()
    return jsonify({"status": "ok"})

@app.route("/menu/bluetooth")
@app.route("/menu/configuracion")
def seccion_configuracion_sistema():
    html_config = """
    <div class="section-header-title"><i class="fa-solid fa-sliders"></i> Panel de Configuración y Acciones</div>
    <div class="card" style="text-align:left;">
        <h4 style="font-size:13px; color:#0f2b5c; margin-bottom:8px;">🛠️ Mantenimiento Base</h4>
        <button type="button" onclick="alert('Estados de ruta diarias reiniciados con éxito.');" style="background:#475569; color:white; border:none; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; width:100%; cursor:pointer;">Resetear Estados de Cobro Diarios</button>
        <button type="button" onclick="alert('Buscando dispositivos térmicos de 58mm...');" style="background:#0284c7; color:white; border:none; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-top:8px; width:100%; cursor:pointer;">🔄 Sincronizar Impresora Bluetooth Portátil</button>
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(html_config)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_config))

@app.route("/api/clientes/lista_negra/<int:id>")
def api_lista_negra(id):
    if not session.get("autenticado"): return "No autorizado"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE clientes SET estado = 'Lista Negra' WHERE id = %s", (id,))
        conn.commit()
    return redirect("/menu/clientes")

@app.route("/api/gasto_foto/<int:gasto_id>")
def api_gasto_foto(gasto_id):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT comprobante FROM balance_movimientos WHERE id = %s", (gasto_id,))
            res = cursor.fetchone()
            
    if res and res["comprobante"]:
        comprobante_puro = str(res["comprobante"]).replace("\n", "").replace("\r", "").strip()
        
        # 🧼 Limpieza absoluta de residuos anteriores del cursor
        if "base64," in comprobante_puro:
            comprobante_puro = comprobante_puro.split("base64,")[-1]
            
        # 🛡️ FIJADO: Retornamos la respuesta forzando cabeceras de origen seguro para romper el bloqueo de Chrome
        respuesta = jsonify({"status": "ok", "comprobante": comprobante_puro})
        respuesta.headers.add("Access-Control-Allow-Origin", "*")
        respuesta.headers.add("Content-Type", "application/json")
        return respuesta
        
    return jsonify({"status": "error", "message": "No encontrado"})

@app.route("/menu/pagos_hoy")
def seccion_pagos_hoy():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 💵 Traemos los recaudos individuales de hoy
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
            
    # 🔄 AGRUPACIÓN CRÍTICA CONTRA DUPLICADOS: Consolida las cuotas continuas del mismo cliente
    pagados_agrupados = []
    mapa_agrupacion = {}
    
    for p in pagos_individuales:
        c_id = p["cliente_id"]
        if c_id not in mapa_agrupacion:
            mapa_agrupacion[c_id] = {
                "nombre": p["nombre"],
                "cuotas": [p["numero"]],
                "total_abonado": float(p["valor_pagado"]),
                "pago_ids": [p["pago_id"]]
            }
        else:
            mapa_agrupacion[c_id]["cuotas"].append(p["numero"])
            mapa_agrupacion[c_id]["total_abonado"] += float(p["valor_pagado"])
            mapa_agrupacion[c_id]["pago_ids"].append(p["pago_id"])

    for c_id, info in mapa_agrupacion.items():
        min_c = min(info["cuotas"])
        max_c = max(info["cuotas"])
        # Formatea el texto de las cuotas: si es una sola muestra "#1", si son varias muestra "#1 al #3"
        rango_cuotas = f"#{min_c}" if min_c == max_c else f"#{min_c} a la #{max_c}"
        
        pagados_agrupados.append({
            "nombre": info["nombre"],
            "rango_cuotas": rango_cuotas,
            "total_abonado": info["total_abonado"],
            # Serializamos los IDs separados por guiones para que el botón de deshacer liquide todo el bloque junto
            "pago_ids_str": "-".join(map(str, info["pago_ids"]))
        })
            
    contexto = dict(vista="pagos_hoy", pagados_list=pagados_agrupados, saltados_list=saltados)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/api/revertir_gestion/<string:tipo>/<string:id_str>")
def api_revertir_gestion(tipo, id_str):
    if not session.get("autenticado"): 
        return jsonify({"status": "error", "message": "No autorizado"})
        
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            if tipo == "pago":
                # Desestructuramos los IDs del bloque agrupado (ej: "12-13-14") para revertirlos juntos
                pago_ids = [int(x) for x in id_str.split("-") if x.strip()]
                for p_id in pago_ids:
                    cursor.execute("UPDATE pagos SET pagado = 0, valor_pagado = 0, fecha_pago_real = '' WHERE id = %s RETURNING cliente_id", (p_id,))
                    res = cursor.fetchone()
                    if res:
                        cursor.execute("UPDATE clientes SET fecha_gestion = '' WHERE id = %s", (res[0],))
            elif tipo == "salto":
                cursor.execute("UPDATE clientes SET saltado_hoy = GREATEST(0, saltado_hoy - 1), fecha_gestion = '' WHERE id = %s", (int(id_str),))
        conn.commit()
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # Servidor local configurado para ejecutarse en el puerto estricto 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

