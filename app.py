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

init_db()
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

# FIJADO: Reestructuración de condicionales Jinja2 para evitar errores de renderizado en Neon/Render
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

                <div style="display:flex; gap:6px; justify-content:flex-end; margin-top:10px;">
                    {% if cuota_pendiente %}
                        <button class="btn-accion btn-pagar" style="background:#10b981;" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})"><i class="fa-solid fa-money-bill-wave"></i> Cobrar</button>
                        <button class="btn-accion btn-nopagar" style="background:#ef4444;" onclick="ejecutarNoPago({{ c.id }})"><i class="fa-solid fa-ban"></i> Saltar</button>
                    {% endif %}
                </div>
            </div>
        {% endfor %}
    </div>

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
"""

CONTENIDO_HTML += """
{% elif vista == 'embed_creditos_maestros' or vista == 'creditos_maestros' or vista == 'creditos' %}
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
            
            <div class="cliente-card-premium" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-mora="{{ 1 if es_mora else 0 }}">
                <div class="card-izquierda">
                    <div class="circulo-frecuencia {% if not es_mora %}al-dia{% elif c.saltado_hoy == 0 %}mora_leve{% endif %}">
                        {{ 'D' if c.frecuencia == 'Diaria' else 'S' }}
                    </div>
                    
                    <div class="info-bloque-texto" style="width:100%;">
                        <div class="id-alias">{{ c.id }} {{ c.referencia or 'Comercio' }}</div>
                        <div class="nombre-real">{{ c.nombre }}</div>
                        
                        <div class="grid-metricas-premium">
                            <div>
                                <span class="metric-lbl">Vr. Cuota</span>
                                <span class="metric-val">${{ "%.2f"|format(c.valor_cuota) }}</span>
                            </div>
                            <div>
                                <span class="metric-lbl">Pendiente</span>
                                <span class="metric-val">{{ cuotas_pagadas }} / {{ total_cuotas }}</span>
                            </div>
                            <div>
                                <span class="metric-lbl">Pago</span>
                                <span class="metric-val">—</span>
                            </div>
                        </div>
                        
                        <div class="fila-utilidades-premium">
                            <button type="button" class="btn-utilidad-foto" onclick="navegarRuta('/menu/balance')">
                                <i class="fa-solid fa-camera"></i>
                            </button>
                            <div class="circulo-check-cuota {% if c.saltado_hoy > 0 %}gestionado{% elif cuotas_pagadas > 0 %}gestionado-ok{% endif %}">
                                {% if c.saltado_hoy > 0 %}{{ c.saltado_hoy }}{% else %}{{ cuotas_pagadas if cuotas_pagadas > 0 else 1 }}{% endif %}
                            </div>
                            <div class="badge-saldo-premium">Saldo <b>${{ "%.2f"|format(saldo_restante) }}</b></div>
                        </div>
                    </div>
                </div>
                
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
    </div>
"""

CONTENIDO_HTML += """
{% elif vista == 'pagos_hoy' %}
    <div class="section-header-title"><i class="fa-solid fa-receipt"></i> Panel de Control - Pagos del Día</div>
    <p style="font-size:11px; color:#64748b; margin-bottom:12px; text-align:left; padding:0 4px;">Audita los movimientos ejecutados hoy.</p>
    
    <div class="card" style="text-align:left; padding:16px;">
        <h4 style="font-size:13px; color:#10b981; margin-bottom:10px;"><i class="fa-solid fa-circle-check"></i> Clientes que SÍ Pagaron Hoy</h4>
        {% if pagados_list %}
            {% for p in pagados_list %}
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding:10px 0; font-size:12px;">
                    <div>
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

    <div class="card" style="text-align:left; padding:16px; margin-top:12px;">
        <h4 style="font-size:13px; color:#f59e0b; margin-bottom:10px;"><i class="fa-solid fa-circle-exclamation"></i> Clientes Saltados / No Pago Hoy</h4>
        {% if saltados_list %}
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
            <button type="submit" class="btn-accion btn-pagar" style="width:100%; padding:12px; margin-top:8px; font-size:13px; font-weight:800;">💾 Guardar e Inicializar Tablas</button>
        </form>
    </div>
"""

CONTENIDO_HTML += """
{% elif vista == 'balance' or vista == 'gastos' %}
    <div class="section-header-title"><i class="fa-solid fa-scale-balanced"></i> Control de Caja y Balance Diario</div>
    <div class="expected-card" style="border-left: 5px solid #10b981; background:#ecfdf5; padding:12px; border-radius:12px; margin-bottom:12px; text-align:left;">
        <div class="kpi-title" style="color:#065f46;">Estado Operativo de Ruta</div>
        <div class="kpi-val" style="color:#065f46; font-size:14px;">Abierta | Base Inicial: ${{ "%.2f"|format(caja_base) }}</div>
    </div>
    
    <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; margin-bottom:12px;">
        <div class="kpi-card" style="border-left:4px solid #10b981; background:white; padding:10px; border-radius:8px;">
            <div class="kpi-title">Cobros (Cuotas)</div>
            <div class="kpi-val" style="color:#10b981; font-size:13px;">+${{ "%.2f"|format(total_cobrado_hoy) }}</div>
        </div>
        <div class="kpi-card" style="border-left:4px solid #00a8cc; background:white; padding:10px; border-radius:8px;">
            <div class="kpi-title">Ventas (Nuevos)</div>
            <div class="kpi-val" style="color:#00a8cc; font-size:13px;">+${{ "%.2f"|format(entradas_totales) }}</div>
        </div>
        <div class="kpi-card" style="border-left:4px solid #ef4444; background:white; padding:10px; border-radius:8px;">
            <div class="kpi-title">Gastos / Salidas</div>
            <div class="kpi-val" style="color:#ef4444; font-size:13px;">-${{ "%.2f"|format(salidas_totales) }}</div>
        </div>
    </div>

    <div class="card" style="background:#0f2b5c; color:white; text-align:center; padding:14px; margin-bottom:12px;">
        <div class="kpi-title" style="color:white; opacity:0.8;">TOTAL EFECTIVO A TENER EN MANO</div>
        <div class="kpi-val" style="color:white; font-size:22px; margin-top:4px; font-weight:800;">${{ "%.2f"|format(efectivo_neto) }}</div>
    </div>

    <div class="card" style="padding:16px; text-align:left;">
        <form id="formBalance" onsubmit="event.preventDefault();">
            <input type="hidden" name="tipo_mov" id="tipo_mov" value="Salida">
            <input type="hidden" id="foto_comprimida_b64" name="foto_comprimida_b64" value="">
            <label>Categoría del Gasto</label>
            <select name="categoria_mov" id="categoria_mov" style="width:100%; padding:10px; border-radius:8px;">
                <option value="Gasolina">⛽ Gasolina / Transporte</option>
                <option value="Almuerzo">🍔 Almuerzo / Alimentación</option>
                <option value="Otros" selected>📦 Otros Gastos</option>
            </select>
            <label>Descripción / Detalle</label>
            <input type="text" name="concepto_mov" id="concepto_mov" required style="width:100%; padding:10px;">
            <label>Monto a Restar ($)</label>
            <input type="number" step="any" name="monto_mov" id="monto_mov" required style="width:100%; padding:10px;">
            <label>📸 Foto Factura / Comprobante</label>
            <input type="file" id="foto_mov" accept="image/*" capture="environment" onchange="procesarImagenEnCaliente()" style="margin-bottom:10px;">
            <button type="button" onclick="procesarYEnviarGasto()" class="btn-accion btn-pagar" style="width:100%; padding:12px;">💾 Guardar Movimiento</button>
        </form>
    </div>

    <div class="card" style="padding:16px; text-align:left;">
        <h4 style="font-size:12px; color:#0f2b5c; margin-bottom:10px;">📋 Historial de Gastos de Hoy</h4>
        {% if gastos_list %}
            {% for g in gastos_list %}
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding:8px 0; font-size:12px;">
                    <div style="flex:1;">
                        <b>[{{ g.categoria }}]</b> {{ g.concepto }}
                        <span style="color:#ef4444; font-weight:bold; margin-left:6px;">-${{ "%.2f"|format(g.monto) }}</span>
                    </div>
                    <div style="display:flex; align-items:center; gap:8px;">
                        {% if g.comprobante and g.comprobante != "" %}
                            <button type="button" onclick="cargarYVerFoto({{ g.id }})" style="padding:4px 8px; font-size:11px; background:#00a8cc; color:white; border:none; border-radius:6px;">📷 Ver</button>
                        {% endif %}
                        <a href="/api/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar?')" style="color:#ef4444; font-weight:bold; text-decoration:none;">🗑️</a>
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p style="font-size:11px; color:#64748b; text-align:center;">No se registran gastos asentados hoy.</p>
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
    <link rel="stylesheet" href="https://cloudflare.com">
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
        .metric-lbl { font-size: 11px !important; color: #94a3b8 !important; font-weight: 700 !important; margin-bottom: 2px !important; }
        .metric-val { font-size: 13px !important; font-weight: 800 !important; color: #334155 !important; }
        .fila-utilidades-premium { display: flex; align-items: center; gap: 8px; margin-top: auto; padding-top: 4px; }
        .btn-utilidad-foto { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; width: 32px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 13px; }
        .circulo-check-cuota { background: #e2e8f0; color: #475569; width: 24px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; border: 1px solid #cbd5e1; }
        .circulo-check-cuota.gestionado { background: #fee2e2; color: #ef4444; border-color: #fca5a5; }
        .circulo-check-cuota.gestionado-ok { background: #dcfce7; color: #10b981; border-color: #bbf7d0; }
        .badge-saldo-premium { font-size: 11px; color: #64748b; }
        .badge-saldo-premium b { color: #1e293b; font-weight: 800; }
        .card-derecha-acciones { display: flex; flex-direction: column; justify-content: space-around; align-items: center; padding-left: 12px; border-left: 1px solid #e2e8f0; min-width: 50px; gap: 4px; }
        .btn-accion-premium { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; cursor: pointer; font-size: 18px; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; }
        .btn-accion-premium.cobrar { color: #10b981; background: #ecfdf5; border-color: #bbf7d0; }
        .btn-accion-premium.saltar { color: #ef4444; background: #fff1f2; border-color: #fca5a5; }
        @media print {
            body * { visibility: hidden; }
            #ticketPrint, #ticketPrint * { visibility: visible; }
            #ticketPrint { position: absolute; left: 0; top: 0; width: 58mm; font-family: monospace; font-size: 11px; }
        }
    </style>
</head>
"""

HTML_TEMPLATE += """
<body>
    <div class="navbar">
        <button class="btn-nav-icon" onclick="toggleDrawer()"><i class="fa-solid fa-bars"></i> Menú</button>
        <div class="nav-title">🌐 AVANTA PAGOS</div>
        <a href="#" onclick="navegarRuta('/nuevo')" class="btn-nav-icon" style="background:#00a8cc;"><i class="fa-solid fa-plus"></i> Nuevo</a>
    </div>

    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <div class="drawer-logo">🌐 AVANTA PAGOS v1.3</div>
        </div>
        <ul class="drawer-menu">
            <li><a href="#" onclick="navegarRuta('/')"><i class="fa-solid fa-map-location-dot"></i> Ruta Principal</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/pagos_hoy')"><i class="fa-solid fa-receipt"></i> Pagos del Día</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/clientes')"><i class="fa-solid fa-address-book"></i> Clientes</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/creditos')"><i class="fa-solid fa-hand-holding-dollar"></i> Créditos Activos</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/balance')"><i class="fa-solid fa-wallet"></i> Balance de Caja</a></li>
            <li><a href="/logout" style="color:#ef4444;"><i class="fa-solid fa-power-off"></i> Cerrar Sesión</a></li>
        </ul>
    </div>

    <div class="main-container" id="mainContent" style="padding:16px;">
        {{ contenido_html | safe }}
    </div>

    <div class="modal" id="modalInfoCliente">
        <div class="modal-content">
            <h3 id="inf_nombre" style="margin-bottom:12px; color:#0f2b5c;">Cargando Ficha...</h3>
            <div class="modal-grid-data">
                <div class="data-box"><div class="data-lbl">Teléfono</div><div class="data-val" id="inf_tel"></div></div>
                <div class="data-box"><div class="data-lbl">Identificación</div><div class="data-val" id="inf_id"></div></div>
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Dirección</div><div class="data-val" id="inf_dir"></div></div>
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Referencia</div><div class="data-val" id="inf_ref"></div></div>
                <div class="data-box"><div class="data-lbl">Valor Cuota</div><div class="data-val" id="inf_v_cuota"></div></div>
                <div class="data-box"><div class="data-lbl">Saldo Actual</div><div class="data-val" id="inf_saldo_act" style="color:#ef4444;"></div></div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:8px;">
                <a href="#" id="btn_llamar" class="btn-accion btn-pagar" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-phone"></i> Llamar</a>
                <a href="#" id="btn_mapa" target="_blank" class="btn-accion btn-pagar" style="padding:10px; text-decoration:none; background:#00a8cc;"><i class="fa-solid fa-location-dot"></i> Ver Mapa</a>
            </div>
            <button onclick="cerrarModal('modalInfoCliente')" class="btn-modal-close">Salir de la Información</button>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content">
            <h3 style="color:#10b981;"><i class="fa-solid fa-circle-check"></i> Pago Procesado</h3>
            <p id="modalMsg" style="margin:10px 0;"></p>
            <a href="#" id="modalWsBtn" target="_blank" class="btn-ws"><i class="fa-brands fa-whatsapp"></i> WhatsApp Nativo</a>
            <button onclick="window.location.reload();" class="btn-modal-close">Continuar</button>
        </div>
    </div>

    <div class="modal" id="modalAbono">
        <div class="modal-content">
            <h3 id="modalAbonoTitulo">✏️ Registrar Pago</h3>
            <input type="hidden" id="abonoClienteId"><input type="hidden" id="abonoNumCuota">
            <input type="number" step="any" id="abonoMontoInput" placeholder="Monto del dinero ($)" style="margin-bottom:10px;">
            <button onclick="confirmarAbono()" class="btn-accion btn-pagar" style="width:100%; padding:12px;">💾 Guardar Recaudo</button>
            <button onclick="cerrarModal('modalAbono')" class="btn-modal-close">Cancelar</button>
        </div>
    </div>

    <div class="modal" id="modalFoto" style="display:none;">
        <div class="modal-content">
            <h3 style="font-size:14px; color:#0f2b5c; margin-bottom:12px;"><i class="fa-solid fa-receipt"></i> Comprobante</h3>
            <div style="background:#f8fafc; border-radius:10px; padding:6px; min-height:180px;">
                <img id="imgComprobante" src="" style="width:100%; max-height:55vh; object-fit:contain; border-radius:8px;">
            </div>
            <button onclick="cerrarModal('modalFoto')" class="btn-modal-close">Cerrar Imagen</button>
        </div>
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
        });
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
                document.getElementById('inf_v_cuota').innerText = '$' + d.data.valor_cuota.toFixed(2);
                document.getElementById('inf_saldo_act').innerText = '$' + d.data.saldo_actual.toFixed(2);
                document.getElementById('btn_llamar').href = 'tel:' + d.data.telefono;
                
                // FIJADO: Sintaxis correcta de deep-linking universal para mapas móviles
                document.getElementById('btn_mapa').href = 'https://google.com' + d.data.latitud + ',' + d.data.longitud;
                document.getElementById('modalInfoCliente').style.display = 'flex';
            }
        });
}
function filtrarClientes() {
    const query = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.cliente-card, .cliente-card-premium').forEach(card => {
        card.style.display = card.getAttribute('data-nombre').includes(query) ? "flex" : "none";
    });
}
function filtrarEstado(tipo) {
    document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('active'));
    if (document.getElementById('f-' + tipo)) document.getElementById('f-' + tipo).classList.add('active');
    document.querySelectorAll('.cliente-card, .cliente-card-premium').forEach(card => {
        const esMora = card.getAttribute('data-mora') === '1';
        if (tipo === 'todos') card.style.display = "flex";
        else if (tipo === 'mora' && esMora) card.style.display = "flex";
        else if (tipo === 'aldia' && !esMora) card.style.display = "flex";
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
    document.getElementById('abonoMontoInput').value = parseFloat(pendiente).toFixed(2);
    document.getElementById('modalAbonoTitulo').innerText = '✏️ Registrar Pago - Cuota #' + numCuota;
    document.getElementById('modalAbono').style.display = 'flex';
}

"""

HTML_TEMPLATE += """
<script>
function confirmarAbono() {
    const clienteId = document.getElementById('abonoClienteId').value;
    const numCuota = document.getElementById('abonoNumCuota').value;
    const monto = parseFloat(document.getElementById('abonoMontoInput').value);
    if (monto > 0) { cerrarModal('modalAbono'); procesarPagoAPI(clienteId, numCuota, monto); }
}

let imagenComprimidaB64 = "";
let fotoListaParaEnviar = false;

function procesarPagoAPI(clienteId, numCuota, monto) {
    fetch('/api/marcar_pago/' + clienteId + '/' + numCuota + '?monto=' + monto)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                document.getElementById('modalMsg').innerText = 'Recaudo guardado para ' + data.recibo.cliente + '.';
                const numPuro = data.recibo.telefono.toString().replace(/[^0-9]/g, '').trim();
                const textoMensaje = encodeURIComponent(data.recibo.mensaje_ws);
                const urlWeb = 'https://wa.me' + numPuro + '?text=' + textoMensaje;
                
                const btnWs = document.getElementById('modalWsBtn');
                btnWs.href = urlWeb;
                document.getElementById('modalWs').style.display = 'flex';
            }
        });
}
function ejecutarNoPago(clienteId) {
    if (confirm('¿Desea saltar el cobro de este cliente por el día de hoy?')) {
        fetch('/api/marcar_no_pago/' + clienteId)
            .then(res => res.json())
            .then(data => { if (data.status === 'ok') { window.location.reload(); } });
    }
}
function procesarImagenEnCaliente() {
    const fileInput = document.getElementById('foto_mov');
    if (!fileInput.files || fileInput.files.length === 0) return;
    const file = fileInput.files;
    const reader = new FileReader();
    reader.onload = function(e) {
        const img = new Image();
        img.onload = function() {
            const canvas = document.createElement('canvas');
            let width = img.width;
            let height = img.height;
            if (width > 600) { height *= 600 / width; width = 600; }
            canvas.width = width; canvas.height = height;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, width, height);
            imagenComprimidaB64 = canvas.toDataURL('image/jpeg', 0.5);
            document.getElementById('foto_comprimida_b64').value = imagenComprimidaB64;
            fotoListaParaEnviar = true;
        };
        img.src = e.target.result;
    };
    reader.readAsDataURL(file);
}
function procesarYEnviarGasto() {
    const fileInput = document.getElementById('foto_mov');
    const tipo = document.getElementById('tipo_mov').value;
    const categoria = document.getElementById('categoria_mov').value;
    const concepto = document.getElementById('concepto_mov').value.trim();
    const monto = document.getElementById('monto_mov').value;
    if (!concepto || !monto) return;
    if (fileInput.files.length > 0 && !fotoListaParaEnviar) {
        setTimeout(procesarYEnviarGasto, 200); return;
    }
    ejecutarDespachoGasto(tipo, categoria, concepto, monto, imagenComprimidaB64);
}
function ejecutarDespachoGasto(tipo, categoria, concepto, monto, fotoB64) {
    fetch('/api/balance/guardar_movimiento', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tipo_mov: tipo, categoria_mov: categoria, concepto_mov: concepto, monto_mov: monto, foto_comprimida_b64: fotoB64 })
    }).then(res => res.json()).then(data => { if (data.status === 'ok') window.location.reload(); });
}
function cargarYVerFoto(gastoId) {
    fetch('/api/gasto_foto/' + gastoId)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok' && data.comprobante) {
                document.getElementById('imgComprobante').src = 'data:image/jpeg;base64,' + data.comprobante;
                document.getElementById('modalFoto').style.display = 'flex';
            }
        });
}
function revertirGestionDia(tipo, id) {
    fetch('/api/revertir_gestion/' + tipo + '/' + id).then(res => res.json()).then(data => { if (data.status === 'ok') window.location.reload(); });
}
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
    ahora = datetime.now()
    fecha_legible = ahora.strftime("%A, %d de %B de %Y")
    
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
        <div class="kpi-card"><div class="kpi-title">Recaudo Hoy / Créditos</div><div class="kpi-val">${total_cobrado_hoy:.2f} / {creditos_nuevos_hoy} Créd.</div></div>
        <div class="kpi-card"><div class="kpi-title">Capital Total en Calle</div><div class="kpi-val" style="color:#0f2b5c;">${capital_en_calle:.2f}</div></div>
    </div>
    <div class="expected-card">
        <div class="kpi-title" style="color:#0369a1;">Recaudo Esperado del Día</div>
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">🔬 Mínimo de Ruta: <b>${debido_minimo_dia:.2f}</b> | Acumulado Mora: <b>${debido_total_acumulado:.2f}</b></div>
    </div>
    <div style="display:flex; gap:6px; margin-bottom:10px; overflow-x:auto;">
        <button onclick="filtrarEstado('todos')" class="btn-filtro active" id="f-todos">👥 Por Cobrar</button>
        <button onclick="filtrarEstado('mora')" class="btn-filtro" id="f-mora">⚠️ Mora</button>
        <button onclick="filtrarEstado('aldia')" class="btn-filtro" id="f-aldia">✅ Al Día</button>
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
        
        js_num_cuota = str(cuota_p["numero"]) if cuota_p else "1"
        js_pendiente = f"{(cuota_p['valor'] - cuota_p['valor_pagado'])}" if cuota_p else "0"
        inicial_frecuencia = 'D' if c['frecuencia'] == 'Diaria' else 'S'
        clase_mora, clase_mora_badge = ("mora-leve", "1") if es_mora_cl else ("al-dia", "0")
        clase_check = "gestionado" if c['saltado_hoy'] > 0 else ("gestionado-ok" if cuotas_pagadas > 0 else "")
        numero_check = str(c['saltado_hoy'] if c['saltado_hoy'] > 0 else (cuotas_pagadas if cuotas_pagadas > 0 else 1))
        
        contexto_dash += f"""
        <div class="cliente-card-premium" id="cliente-card-{c['id']}" data-nombre="{c['nombre'].lower()}" data-mora="{clase_mora_badge}">
            <div class="card-izquierda">
                <div class="circulo-frecuencia {clase_mora}">{inicial_frecuencia}</div>
                <div class="info-bloque-texto" style="width:100%;">
                    <div class="id-alias" onclick="verFichaCliente({c['id']})" style="cursor:pointer;">{c['id']} {c['referencia'] or 'Comercio'}</div>
                    <div class="nombre-real">{c['nombre']}</div>
                    <div class="grid-metricas-premium">
                        <div class="metric-col"><span class="metric-lbl">Vr. Cuota</span><span class="metric-val">${c['valor_cuota']:.2f}</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pendiente</span><span class="metric-val">{cuotas_pagadas}.0 / {c['cuotas']}.0</span></div>
                        <div class="metric-col"><span class="metric-lbl">Pago</span><span class="metric-val">—</span></div>
                    </div>
                    <div class="fila-utilidades-premium">
                        <button type="button" class="btn-utilidad-foto" onclick="abrirModalAbono({c['id']}, {js_num_cuota}, {js_pendiente}, {c['valor_cuota']})">📸</button>
                        <div class="circulo-check-cuota {clase_check}">{numero_check}</div><div class="badge-saldo-premium">Saldo <b>${saldo_restante:.2f}</b></div>
                    </div>
                </div>
            </div>
            <div class="card-derecha-acciones">
                <button type="button" class="btn-accion-premium cobrar" title="Cobrar Cuota" onclick="abrirModalAbono({c['id']}, {js_num_cuota}, {js_pendiente}, {c['valor_cuota']})">💵✔️</button>
                <button type="button" class="btn-accion-premium saltar" title="Saltar Cliente" onclick="ejecutarNoPago({c['id']})">💵❌</button>
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
            cursor.execute(
                """
                SELECT c.id, c.nombre, c.monto, c.interes_porcentaje, c.monto_total, c.valor_cuota, c.frecuencia, c.estado,
                       COALESCE(SUM(p.valor_pagado), 0) AS total_recaudado
                FROM clientes c LEFT JOIN pagos p ON c.id = p.cliente_id
                GROUP BY c.id ORDER BY c.nombre ASC
                """
            )
            todos_clientes = cursor.fetchall()
            
    creditos_procesados = [dict(cr) for cr in todos_clientes]
    for c_dict in creditos_procesados:
        c_dict["saldo_pendiente"] = max(0.0, float(c_dict["monto_total"]) - float(c_dict["total_recaudado"]))
        
    html_f = render_template_string(CONTENIDO_HTML, vista="clientes_financieros", lista_creditos=creditos_procesados)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return html_f
    return render_template_string(HTML_TEMPLATE, contenido_html=html_f)

@app.route("/menu/creditos")
def seccion_creditos_activos():
    if not session.get("autenticado"): return "Sesión expirada"
    filtro_estado = request.args.get("filtro", "Activo")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE estado = %s ORDER BY nombre ASC", (filtro_estado,))
            todos = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Activo'")
            total_activos = int(cursor.fetchone()["total"])
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Inactivo'")
            total_inactivos = int(cursor.fetchone()["total"])
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Lista Negra'")
            total_negra = int(cursor.fetchone()["total"])
            
    clientes_list = []
    for t in todos:
        t_dict = dict(t)
        with get_db() as conn2:
            with conn2.cursor() as cursor2:
                cursor2.execute("SELECT * FROM pagos WHERE cliente_id = %s", (t_dict["id"],))
                t_dict["pagos"] = cursor2.fetchall()
        clientes_list.append(t_dict)
        
    html_f = render_template_string(CONTENIDO_HTML, vista="creditos_maestros", clientes=clientes_list, total_activos=total_activos, total_inactivos=total_inactivos, total_negra=total_negra, filtro_estado=filtro_estado)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return html_f
    return render_template_string(HTML_TEMPLATE, contenido_html=html_f)

@app.route("/menu/balance")
def seccion_balance_maestro():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base'")
            res_caja = cursor.fetchone()
            caja_base = float(res_caja["valor"]) if res_caja else 100.00
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
            total_cobrado_hoy = float(cursor.fetchone()["total"])
            cursor.execute("SELECT COALESCE(SUM(monto), 0) AS total FROM balance_movimientos WHERE tipo = 'Entrada' AND fecha = %s", (hoy_str,))
            entradas_totales = float(cursor.fetchone()["total"])
            cursor.execute("SELECT * FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s ORDER BY id DESC", (hoy_str,))
            movimientos_gastos = cursor.fetchall()
            
    salidas_totales = sum(float(g["monto"]) for g in movimientos_gastos)
    efectivo_neto = caja_base + total_cobrado_hoy + entradas_totales - salidas_totales
    gastos_clean = [dict(g) for g in movimientos_gastos]
    
    html_f = render_template_string(CONTENIDO_HTML, vista="balance", caja_base=caja_base, total_cobrado_hoy=total_cobrado_hoy, entradas_totales=entradas_totales, salidas_totales=salidas_totales, efectivo_neto=efectivo_neto, gastos_list=gastos_clean)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return html_f
    return render_template_string(HTML_TEMPLATE, contenido_html=html_f)

@app.route("/menu/pagos_hoy")
def seccion_pagos_hoy():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT p.id AS pago_id, c.id AS cliente_id, c.nombre, p.numero, p.valor_pagado FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.fecha_pago_real = %s AND p.pagado = 1 ORDER BY c.nombre ASC", (hoy_str,))
            pagos_ind = cursor.fetchall()
            cursor.execute("SELECT id AS cliente_id, nombre, saltado_hoy FROM clientes WHERE fecha_gestion = %s AND estado = 'Activo'", (hoy_str,))
            saltados = cursor.fetchall()
            
    mapa = {}
    for p in pagos_ind:
        cid = p["cliente_id"]
        if cid not in mapa:
            mapa[cid] = {"nombre": p["nombre"], "cuotas": [p["numero"]], "total": float(p["valor_pagado"]), "ids": [p["pago_id"]]}
        else:
            mapa[cid]["cuotas"].append(p["numero"])
            mapa[cid]["total"] += float(p["valor_pagado"])
            mapa[cid]["ids"].append(p["pago_id"])
            
    agrupados = []
    for cid, info in mapa.items():
        rango = f"#{min(info['cuotas'])}" if min(info['cuotas']) == max(info['cuotas']) else f"#{min(info['cuotas'])} a #{max(info['cuotas'])}"
        agrupados.append({"nombre": info["nombre"], "rango_cuotas": rango, "total_abonado": info["total"], "pago_ids_str": "-".join(map(str, info["ids"]))})
        
    saltados_list = [dict(s) for s in saltados if s["cliente_id"] not in mapa]
    html_f = render_template_string(CONTENIDO_HTML, vista="pagos_hoy", pagados_list=agrupados, saltados_list=saltados_list)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return html_f
    return render_template_string(HTML_TEMPLATE, contenido_html=html_f)

@app.route("/nuevo")
def seccion_nuevo_credito():
    if not session.get("autenticado"): return redirect("/login")
    hoy_str = date.today().isoformat()
    html_f = render_template_string(CONTENIDO_HTML, vista="nuevo", hoy_str=hoy_str, cliente=None)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return html_f
    return render_template_string(HTML_TEMPLATE, contenido_html=html_f)

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
                cursor.execute("SELECT COALESCE(MAX(orden), 0) AS max_o FROM clientes")
                max_orden = cursor.fetchone()["max_o"]
                cursor.execute(
                    """
                    INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, estado)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo') RETURNING id
                    """, (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat())
                )
                cliente_id = cursor.fetchone()["id"]
                
                acumulado = 0.0
                for num in range(1, cuotas + 1):
                    valor_cuota_real = float(round(monto_total - acumulado, 2)) if num == cuotas else valor_base_cuota
                    acumulado += valor_cuota_real
                    f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frecuencia)
                    cursor.execute("INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado) VALUES (%s, %s, %s, %s, 0)", (cliente_id, num, f_cuota.isoformat(), valor_cuota_real))
            conn.commit()
    except Exception as e:
        print(f"Error: {e}")
    return redirect("/")

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    monto = float(request.args.get("monto", 0))
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE pagos SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s WHERE cliente_id = %s AND numero = %s", (monto, hoy_str, cliente_id, num_cuota))
            cursor.execute("UPDATE clientes SET fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
            cursor.execute("SELECT nombre, telefono, monto_total FROM clientes WHERE id = %s", (cliente_id,))
            c = cursor.fetchone()
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE cliente_id = %s", (cliente_id,))
            total_p = float(cursor.fetchone())
            saldo = max(0.0, c["monto_total"] - total_p)
        conn.commit()
    msg = f"🧾 *AVANTA*\\nCliente: {c['nombre']}\\nMonto: ${monto:.2f}\\nSaldo: ${saldo:.2f}"
    return jsonify({"status": "ok", "recibo": {"cliente": c["nombre"], "telefono": c["telefono"] or "", "mensaje_ws": msg}})

@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE clientes SET saltado_hoy = saltado_hoy + 1, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route("/api/balance/guardar_movimiento", methods=["POST"])
def balance_guardar_movimiento():
    datos = request.get_json() or {}
    tipo = datos.get("tipo_mov", "Salida")
    cat = datos.get("categoria_mov", "Otros")
    con = datos.get("concepto_mov", "").strip()
    monto = float(datos.get("monto_mov", "0"))
    b64 = datos.get("foto_comprimida_b64", "").strip().split("base64,")[-1]
    hoy_str = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante) VALUES (%s, %s, %s, %s, %s, %s)", (tipo, cat, con, monto, hoy_str, b64))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route("/api/gasto_foto/<int:gasto_id>")
def api_gasto_foto(gasto_id):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT comprobante FROM balance_movimientos WHERE id = %s", (gasto_id,))
            res = cursor.fetchone()
    b64 = res["comprobante"].strip().split("base64,")[-1] if res and res["comprobante"] else ""
    return jsonify({"status": "ok", "comprobante": b64})

@app.route("/api/cliente_info/<int:id>")
def api_cliente_info(id):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (id,))
            c = dict(cursor.fetchone())
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE cliente_id = %s", (id,))
            p = float(cursor.fetchone())
    c["saldo_actual"] = max(0.0, c["monto_total"] - p)
    return jsonify({"status": "ok", "data": c})

@app.route("/api/revertir_gestion/<string:tipo>/<string:id_str>")
def api_revertir_gestion(tipo, id_str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            if tipo == "pago":
                p_ids = [int(x) for x in id_str.split("-") if x.strip()]
                for p_id in p_ids:
                    cursor.execute("UPDATE pagos SET pagado = 0, valor_pagado = 0, fecha_pago_real = '' WHERE id = %s RETURNING cliente_id", (p_id,))
                    res = cursor.fetchone()
                    if res: cursor.execute("UPDATE clientes SET fecha_gestion = '' WHERE id = %s", (res,))
            elif tipo == "salto":
                cursor.execute("UPDATE clientes SET saltado_hoy = GREATEST(0, saltado_hoy - 1), fecha_gestion = '' WHERE id = %s", (int(id_str),))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route("/mover/<int:cliente_id>/<string:direccion>")
def mover(cliente_id, direccion):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, orden FROM clientes ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            idx = next((i for i, c in enumerate(clientes) if c["id"] == cliente_id), None)
            if idx is not None:
                if direccion == "subir" and idx > 0:
                    act, ant = clientes[idx], clientes[idx - 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (ant["orden"], act["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (act["orden"], ant["id"]))
                elif direccion == "bajar" and idx < len(clientes) - 1:
                    act, sig = clientes[idx], clientes[idx + 1]
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (sig["orden"], act["id"]))
                    cursor.execute("UPDATE clientes SET orden = %s WHERE id = %s", (act["orden"], sig["id"]))
        conn.commit()
    return redirect("/")

@app.route("/api/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    with get_db() as conn:
        with conn.cursor() as cursor: cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (gasto_id,))
        conn.commit()
    return redirect("/menu/balance")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
