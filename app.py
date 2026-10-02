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

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_R4oN0OiVIQcB@ep-weathered-night-b4hv7m66.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

def get_db():
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
                    longitud TEXT DEFAULT ''
                )
                """
            )
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS direccion TEXT DEFAULT ''")
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS referencia TEXT DEFAULT ''")
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS identificacion TEXT DEFAULT ''")
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'Activo'")
            cursor.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS enrutado_forzado INTEGER NOT NULL DEFAULT 0")

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
def obtener_clientes_ruta_hoy():
    conn = get_db()
    hoy_str = date.today().isoformat()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
        clientes = cursor.fetchall()
        if not clientes:
            conn.close()
            return []
        cliente_ids = [c["id"] for c in clientes]
        placeholders = ",".join("%s" for _ in cliente_ids)
        cursor.execute(f"SELECT * FROM pagos WHERE cliente_id IN ({placeholders}) ORDER BY numero ASC", tuple(cliente_ids))
        pagos = cursor.fetchall()

    pagos_por_cliente = defaultdict(list)
    for p in pagos:
        pagos_por_cliente[p["cliente_id"]].append(dict(p))

    resultado = []
    for c in clientes:
        c_dict = dict(c)
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
    conn.close()
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
            <div class="logo-subtitle">PAGOS v1.3</div>
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

    <div style="display:flex; gap:6px; margin-bottom:10px; overflow-x:auto;">
        <button onclick="filtrarEstado('todos')" class="btn-filtro active" id="f-todos">👥 Por Cobrar ({{ clientes | length }})</button>
        <button onclick="filtrarEstado('mora')" class="btn-filtro" id="f-mora">⚠️ Mora</button>
        <button onclick="filtrarEstado('aldia')" class="btn-filtro" id="f-aldia">✅ Al Día</button>
    </div>

    <div id="clientesContainer">
        {% for c in clientes %}
            {% set pagado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo = c.monto_total - pagado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set es_mora = c.cuotas_atrasadas > 0 %}

            <div class="card cliente-card" id="cliente-card-{{ c.id }}" data-nombre="{{ c.nombre | lower }}" data-id="{{ c.identificacion or '' }}" data-mora="{{ 1 if es_mora else 0 }}">
                <div class="flex-between">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <div style="display:flex; flex-direction:column; gap:2px;">
                            <a href="/mover/{{ c.id }}/subir" style="text-decoration:none; font-size:10px; background:#f1f5f9; padding:2px 4px; border-radius:3px;">⬆️</a>
                            <a href="/mover/{{ c.id }}/bajar" style="text-decoration:none; font-size:10px; background:#f1f5f9; padding:2px 4px; border-radius:3px;">⬇️</a>
                        </div>
                        <div>
                            <div style="display:flex; align-items:center; gap:6px;">
                                <span style="font-size:14px; font-weight:800; color:#0f2b5c; cursor:pointer;" onclick="verFichaCliente({{ c.id }})">{{ c.nombre }}</span>
                                {% if es_mora %}
                                    <span class="badge-mora">MORA</span>
                                {% else %}
                                    <span class="badge-al-dia">AL DÍA</span>
                                {% endif %}
                            </div>
                            <div style="font-size:11px; color:#6b7280; margin-top:2px;">
                                Ref: <b>{{ c.referencia or 'Ninguna' }}</b> | Tel: <b>{{ c.telefono or 'N/A' }}</b>
                            </div>
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:10px; color:#6b7280; font-weight:bold;">Cuota:</div>
                        <div style="font-size:16px; font-weight:800; color:#0f2b5c;">${{ "%.2f"|format(c.valor_cuota) }}</div>
                    </div>
                </div>

                <div style="display:flex; gap:6px; justify-content:flex-end; margin-top:10px;">
                    {% if cuota_pendiente %}
                        <button class="btn-accion btn-pagar" onclick="ejecutarPago({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})">Cobrar</button>
                        <button class="btn-accion btn-abono" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})">Abono</button>
                        <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({{ c.id }})">Saltar</button>
                    {% endif %}
                </div>
            </div>
        {% endfor %}
    </div>

{% elif vista == 'clientes' or vista == 'creditos' %}
    <div class="section-header-title"><i class="fa-solid fa-users"></i> CONTROL MAESTRO DE CLIENTES</div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
        {% for cl in todos %}
            <div class="card cliente-card" data-nombre="{{ cl.nombre | lower }}">
                <div class="flex-between">
                    <div>
                        <span style="font-weight:800; color:#0f2b5c; font-size:14px;">{{ cl.nombre }}</span>
                        <span style="font-size:9px; padding:2px 6px; border-radius:4px; font-weight:800; background:#f1f5f9; color:#475569; margin-left:4px;">{{ cl.estado }}</span>
                    </div>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <!-- 🔄 Enlace dinámico funcional de renovación -->
                        <button class="btn-accion btn-abono" onclick="navegarRuta('/mover_renovacion/{{ cl.id }}')" style="border:none; padding:8px 12px;"><i class="fa-solid fa-rotate"></i> Renovar</button>
                        <!-- 🗑️💥 Botón de borrado estilizado en rojo con icono y emoji -->
                        <a href="/api/eliminar_cliente/{{ cl.id }}" onclick="return confirm('¿Eliminar cliente permanentemente de la ruta?')" class="btn-accion btn-nopagar" style="text-decoration:none; padding:8px 12px; background:#dc2626;"><i class="fa-solid fa-trash-can"></i> 🗑️💥 Borrar</a>
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>

{% elif vista == 'nuevo' or vista == 'renovar' %}
    <h3 style="margin-top:0; color:#0f2b5c;">👤 Registro de Crédito / Venta</h3>
    <div class="card" style="padding:16px;">
        <form action="/guardar" method="POST">
            {% if vista == 'renovar' %}<input type="hidden" name="cliente_id" value="{{ cliente.id }}">{% endif %}
            <label>Nombre del Cliente</label><input type="text" name="nombre" value="{{ cliente.nombre if cliente else '' }}" required>
            <label>WhatsApp</label><input type="text" name="telefono" value="{{ cliente.telefono if cliente else '' }}" required>
            <label>Identificación</label><input type="text" name="identificacion" value="{{ cliente.identificacion if cliente else '' }}">
            <label>Dirección</label><input type="text" name="direccion" value="{{ cliente.direccion if cliente else '' }}">
            <label>Referencia Local</label><input type="text" name="referencia" value="{{ cliente.referencia if cliente else '' }}">
            <label>Monto Financiado ($)</label><input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            <label>Interés (%)</label><input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            <label>Cuotas</label><input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            <div id="simulacionText" style="font-weight:bold; color:#0369a1; margin:6px 0;">$0.00 / cuota</div>
            <label>Frecuencia</label>
            <select name="frecuencia"><option value="Diaria">Diaria</option><option value="Semanal">Semanal</option></select>
            <label>Fecha de Inicio</label><input type="date" name="fecha_inicio" value="{{ hoy_str }}" required>
            <input type="hidden" id="input_latitud" name="latitud"><input type="hidden" id="input_longitud" name="longitud">
            <button type="submit" class="btn-primary" style="background:#0f2b5c; color:white; border:none; font-weight:bold; padding:12px; margin-top:8px;">💾 Guardar Crédito</button>
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
                -${{ "%.2f"|format(salidas_totales) }}
                Efectivo Líquido Neto
                ${{ "%.2f"|format(efectivo_neto) }}
                Registrar Movimiento de Flujo
                Tipo de Flujo
                📥 Entrada / Inversión Capital
                📤 Salida / Registro de Gasto
                Categoría del Egreso
                ⛽ Combustible / Gasolina
                🍔 Alimentación / Almuerzo
                🛠️ Mantenimiento Vehículo
                🎒 Viáticos de Ruta
                💵 Sueldos / Pagos
                📦 Otros Egresos
                Descripción / Detalle
                Monto ($)
                📸 Foto Factura / Comprobante
                Guardar Registro
                📋 Historial de Movimientos de Hoy
            {% if gastos_list %}
            {% for g in gastos_list %}

        [{{ g.categoria }}] {{ g.concepto }}

        {% if g.tipo == 'Entrada' %}+{% else %}-{% endif %}${{ "%.2f"|format(g.monto) }}

            {% if g.comprobante %}
            📷 Ver
    {% endif %}
        🗑️

    {% endfor %}
    {% else %}
        No hay flujos asentados hoy.
    {% endif %}

{% endif %}
"""
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AVANTA PAGOS v1.3</title>
    <link href="https://googleapis.com" rel="stylesheet">
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
        .toggle-section { background: white; border: 1px solid #e2e8f0; padding: 6px; border-radius: 12px; display: flex; gap: 6px; margin-bottom: 12px; }
        .btn-toggle { flex: 1; border: none; padding: 8px; border-radius: 8px; font-size: 12px; font-weight: 800; color: #64748b; background: #f1f5f9; }
        .btn-toggle.active { background: #0f2b5c; color: white; }
        .search-box { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 10px; margin-bottom: 12px; font-size: 13px; outline: none; }
        .card { background: white; padding: 14px; border-radius: 12px; margin-bottom: 10px; border: 1px solid #e2e8f0; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .btn-accion { border: none; padding: 8px 12px; border-radius: 8px; font-weight: 800; font-size: 11px; cursor: pointer; color: white; text-align: center; }
        .btn-pagar { background: #10b981; } .btn-abono { background: #00a8cc; } .btn-nopagar { background: #ef4444; }
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
            <li><a href="#" onclick="navegarRuta('/menu/clientes')"><i class="fa-solid fa-address-book"></i> Clientes</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/creditos')"><i class="fa-solid fa-hand-holding-dollar"></i> Créditos Activos</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/balance')"><i class="fa-solid fa-wallet"></i> Balance de Caja</a></li>
            <li><a href="/logout" style="color:#ef4444;"><i class="fa-solid fa-power-off"></i> Cerrar Sesión</a></li>
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
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Dirección</div><div class="data-val" id="inf_dir"></div></div>
                <div class="data-box" style="grid-column:span 2;"><div class="data-lbl">Referencia</div><div class="data-val" id="inf_ref"></div></div>
                <div class="data-box"><div class="data-lbl">Fecha Crédito</div><div class="data-val" id="inf_f_ini"></div></div>
                <div class="data-box"><div class="data-lbl">Vencimiento</div><div class="data-val" id="inf_f_venc"></div></div>
                <div class="data-box"><div class="data-lbl">Valor Cuota</div><div class="data-val" id="inf_v_cuota"></div></div>
                <div class="data-box"><div class="data-lbl">Saldo Actual</div><div class="data-val" id="inf_saldo_act" style="color:#ef4444;"></div></div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:8px;">
                <a href="#" id="btn_llamar" class="btn-accion btn-abono" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-phone"></i> Llamar</a>
                <a href="#" id="btn_mapa" target="_blank" class="btn-accion btn-pagar" style="padding:10px; text-decoration:none;"><i class="fa-solid fa-route"></i> Mapa</a>
            </div>
            <button onclick="cerrarModal('modalInfoCliente')" class="btn-modal-close">Cerrar Ficha</button>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content">
            <h3 style="color:#10b981;"><i class="fa-solid fa-circle-check"></i> Pago Procesado</h3>
            <p id="modalMsg" style="margin:10px 0;"></p>
            <a href="#" id="modalWsBtn" target="_blank" class="btn-ws"><i class="fa-brands fa-whatsapp"></i> WhatsApp Nativo</a>
            <button onclick="imprimirTicket()" class="btn-primary" style="background:#0f2b5c; margin-top:8px; color:white; border:none;"><i class="fa-solid fa-print"></i> Imprimir Recibo</button>
            <button onclick="window.location.reload();" class="btn-modal-close">Continuar</button>
        </div>
    </div>

    <div class="modal" id="modalAbono">
        <div class="modal-content">
            <h3>✏️ Registrar Abono Parcial</h3>
            <input type="hidden" id="abonoClienteId"><input type="hidden" id="abonoNumCuota">
            <input type="number" step="any" id="abonoMontoInput" placeholder="Monto del dinero ($)">
            <button onclick="confirmarAbono()" class="btn-primary" style="margin-top:10px; background:#00a8cc; color:white; border:none;">💾 Guardar</button>
            <button onclick="cerrarModal('modalAbono')" class="btn-modal-close">Cancelar</button>
        </div>
    </div>

    <div class="modal" id="modalFoto">
        <div class="modal-content" style="max-width:90%;">
            <h3 style="margin-top:0; font-size:14px;">📸 Comprobante Contable</h3>
            <img id="imgComprobante" src="" style="width:100%; max-height:60vh; object-fit:contain; border-radius:8px; border:1px solid #cbd5e1;">
            <button onclick="cerrarModal('modalFoto')" class="btn-modal-close">Cerrar Imagen</button>
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
            if (url === '/nuevo') activarGpsNativo();
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
function ejecutarPago(clienteId, numCuota, monto) { procesarPagoAPI(clienteId, numCuota, monto); }
function abrirModalAbono(clienteId, numCuota, pendiente, valorCuota) {
    document.getElementById('abonoClienteId').value = clienteId;
    document.getElementById('abonoNumCuota').value = numCuota;
    document.getElementById('abonoMontoInput').value = pendiente;
    document.getElementById('modalAbono').style.display = 'flex';
}
function confirmarAbono() {
    const clienteId = document.getElementById('abonoClienteId').value;
    const numCuota = document.getElementById('abonoNumCuota').value;
    const monto = parseFloat(document.getElementById('abonoMontoInput').value);
    if (monto > 0) { cerrarModal('modalAbono'); procesarPagoAPI(clienteId, numCuota, monto); }
}
function procesarPagoAPI(clienteId, numCuota, monto) {
    fetch('/api/marcar_pago/' + clienteId + '/' + numCuota + '?monto=' + monto)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                document.getElementById('modalMsg').innerText = 'Recaudo guardado para ' + data.recibo.cliente + '.';
                document.getElementById('tFecha').innerText = new Date().toLocaleDateString();
                document.getElementById('tCliente').innerText = data.recibo.cliente;
                document.getElementById('tCuota').innerText = data.recibo.cuota;
                document.getElementById('tMonto').innerText = data.recibo.monto.toFixed(2);
                document.getElementById('tSaldo').innerText = data.recibo.saldo.toFixed(2);
                const numPuro = data.recibo.telefono.toString().replace(/[^0-9]/g, '').trim();
                document.getElementById('modalWsBtn').href = 'https://whatsapp.com' + numPuro + '&text=' + data.recibo.mensaje_ws;
                document.getElementById('modalWs').style.display = 'flex';
            }
        });
}
function ejecutarNoPago(clienteId) {
    fetch('/api/marcar_no_pago/' + clienteId).then(res => res.json()).then(data => {
        if (data.status === 'ok') window.location.reload();
    });
}
function verFoto(srcBase64) {
    document.getElementById('imgComprobante').src = srcBase64;
    document.getElementById('modalFoto').style.display = 'flex';
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
        with conn.cursor() as cursor:
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'usuario'")
            usr_real = cursor.fetchone()[0]
            cursor.execute("SELECT valor FROM configuracion WHERE llave = 'clave'")
            clv_real = cursor.fetchone()[0]
        conn.close()
        if usuario_ingresado == usr_real and clave_ingresada == clv_real:
            session["autenticado"] = True
            session["usuario"] = usuario_ingresado
            return redirect("/")
        else:
            return render_template_string(LOGIN_HTML, error="Credenciales incorrectas.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
def dashboard_principal():
    if not session.get("autenticado"): return redirect("/login")
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
        total_cobrado_hoy = float(cursor.fetchone()[0])
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s", (hoy_str,))
        total_gastos_hoy = float(cursor.fetchone()[0])
        cursor.execute("SELECT COUNT(*) FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
        creditos_nuevos_hoy = int(cursor.fetchone()[0])
    conn.close()

    clientes_ruta = obtener_clientes_ruta_hoy()
    debido_minimo_dia = sum(cl["valor_cuota"] for cl in clientes_ruta)
    capital_en_calle = sum(max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"])) for c in clientes_ruta)

    contexto_dash = f"""
    <div class="date-badge"><i class="fa-solid fa-calendar-day"></i> Ruta del Día</div>
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
        <div class="kpi-title" style="color:#0369a1;">Recaudo Mínimo Esperado</div>
        <div class="kpi-val" style="color:#0369a1; font-size:14px;">🔬 Mínimo de Ruta: <b>${debido_minimo_dia:.2f}</b></div>
    </div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar por nombre..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
    """
    for c in clientes_ruta:
        cuota_p = next((p for p in c["pagos"] if p["pagado"] == 0), None)
        es_mora_cl = c.get("cuotas_atrasadas", 0) > 0
        contexto_dash += f"""
        <div class="card cliente-card" data-nombre="{c['nombre'].lower()}" data-mora="{"1" if es_mora_cl else "0"}">
            <div class="flex-between">
                <div style="cursor:pointer;" onclick="verFichaCliente({c['id']})">
                    <span style="font-size:14px; font-weight:800; color:#0f2b5c;">{c['nombre']}</span>
                    {"<span class='badge-mora'>MORA</span>" if es_mora_cl else "<span class='badge-al-dia'>AL DÍA</span>"}
                    <div style="font-size:11px; color:#64748b; margin-top:2px;">Ref: {c['referencia'] or 'N/A'}</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:15px; font-weight:800; color:#0f2b5c;">${c['valor_cuota']:.2f}</div>
                </div>
            </div>
            <div style="display:flex; justify-content:flex-end; gap:6px; margin-top:10px;">
                <button class="btn-accion btn-pagar" onclick="ejecutarPago({c['id']}, {cuota_p['numero'] if cuota_p else 1}, {cuota_p['valor'] - cuota_p['valor_pagado'] if cuota_p else 0})">Cobrar</button>
                <button class="btn-accion btn-abono" onclick="abrirModalAbono({c['id']}, {cuota_p['numero'] if cuota_p else 1}, {cuota_p['valor'] - cuota_p['valor_pagado'] if cuota_p else 0}, {c['valor_cuota']})">Abono</button>
                <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({c['id']})">Saltar</button>
            </div>
        </div>
        """
    contexto_dash += "</div>"
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return render_template_string(contexto_dash)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(contexto_dash))
@app.route("/menu/clientes")
@app.route("/menu/creditos")
def seccion_clientes():
    if not session.get("autenticado"): return "Sesión expirada"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes ORDER BY nombre ASC")
        todos = cursor.fetchall()
    conn.close()
    contexto = dict(vista="clientes", todos=todos)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/menu/balance")
def seccion_balance():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base'")
        caja_base = float(cursor.fetchone()[0])
        cursor.execute("SELECT * FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
        gastos_list = cursor.fetchall()
    conn.close()
    entradas_totales = sum(m["monto"] for m in gastos_list if m["tipo"] == "Entrada")
    salidas_totales = sum(m["monto"] for m in gastos_list if m["tipo"] == "Salida")
    efectivo_neto = caja_base + entradas_totales - salidas_totales
    contexto = dict(vista="balance", caja_base=caja_base, gastos_list=gastos_list, entradas_totales=entradas_totales, salidas_totales=salidas_totales, efectivo_neto=efectivo_neto)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/nuevo")
@app.route("/mover_renovacion/<int:cliente_id>")
def seccion_nuevo_credito(cliente_id=None):
    if not session.get("autenticado"): return redirect("/login")
    hoy_str = date.today().isoformat()
    cliente = None
    if cliente_id:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
            cliente = cursor.fetchone()
        conn.close()
    contexto = dict(vista="nuevo", hoy_str=hoy_str, cliente=cliente)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/guardar", methods=["POST"])
def guardar_nuevo_cliente():
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
        monto_total = round(monto * (1 + (interes_porcentaje / 100)), 2)
        valor_base_cuota = round(monto_total / cuotas, 2)
        fecha_vencimiento_dt = calcular_fecha(fecha_inicio_dt, cuotas, frecuencia)
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes")
            max_orden = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO clientes (orden, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, fecha_vencimiento, estado, saltado_hoy, fecha_gestion, latitud, longitud)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo', 0, '', %s, %s) RETURNING id
                """,
                (max_orden + 1, nombre, telefono, direccion, referencia, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base_cuota, fecha_inicio_dt.isoformat(), fecha_vencimiento_dt.isoformat(), latitud, longitud)
            )
            cliente_id = cursor.fetchone()[0]
            acumulado = 0.0
            for num in range(1, cuotas + 1):
                valor_cuota_real = round(monto_total - acumulado, 2) if num == cuotas else valor_base_cuota
                acumulado += valor_cuota_real
                f_cuota = calcular_fecha(fecha_inicio_dt, num - 1, frecuencia)
                cursor.execute("INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real) VALUES (%s, %s, %s, %s, 0, 0, '')", (cliente_id, num, f_cuota.isoformat(), valor_cuota_real))
        conn.commit()
        conn.close()
    except Exception as e: print(e)
    return redirect("/")

@app.route("/api/balance/guardar_movimiento", methods=["POST"])
def balance_guardar_movimiento():
    tipo = request.form.get("tipo_mov")
    categoria = request.form.get("categoria_mov", "Otros")
    concepto = request.form.get("concepto_mov", "").strip()
    monto = float(request.form.get("monto_mov", "0"))
    hoy_str = date.today().isoformat()
    if not concepto: concepto = "Flujo de " + categoria
    file = request.files.get("foto_mov")
    base64_str = ""
    if file and file.filename != "":
        base64_str = "data:" + file.content_type + ";base64," + base64.b64encode(file.read()).decode("utf-8")
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante) VALUES (%s, %s, %s, %s, %s, %s)", (tipo, categoria, concepto, monto, hoy_str, base64_str))
    conn.commit()
    conn.close()
    return redirect("/menu/balance")

@app.route("/api/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    conn = get_db()
    with conn.cursor() as cursor: cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (gasto_id,))
    conn.commit()
    conn.close()
    return redirect("/menu/balance")

@app.route("/api/cliente_info/<int:id>")
def api_cliente_info(id):
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes WHERE id = %s", (id,))
        cl = cursor.fetchone()
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY numero ASC", (id,))
        pgs = cursor.fetchall()
    conn.close()
    if not cl: return jsonify({"status": "error"})
    saldo = cl["monto_total"] - sum(p["valor_pagado"] for p in pgs)
    data = {
        "nombre": cl["nombre"], "telefono": cl["telefono"] or 'N/A',
        "identificacion": cl["identificacion"] or 'N/A', "direccion": cl["direccion"] or 'N/A',
        "referencia": cl["referencia"] or 'N/A', "fecha_inicio": cl["fecha_inicio"],
        "fecha_vencimiento": cl["fecha_vencimiento"], "valor_cuota": cl["valor_cuota"],
        "saldo_actual": saldo, "latitud": cl["latitud"] or "0", "longitud": cl["longitud"] or "0"
    }
    return jsonify({"status": "ok", "data": data})

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    monto_ingresado = float(request.args.get("monto", 0))
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s AND numero = %s", (cliente_id, num_cuota))
        pago = cursor.fetchone()
        if pago:
            nuevo_valor_pagado = pago["valor_pagado"] + monto_ingresado
            esta_pagado = 1 if nuevo_valor_pagado >= (pago["valor"] - 0.01) else 0
            cursor.execute("UPDATE pagos SET valor_pagado = %s, pagado = %s, fecha_pago_real = %s WHERE cliente_id = %s AND numero = %s", (nuevo_valor_pagado, esta_pagado, hoy_str, cliente_id, num_cuota))
            cursor.execute("UPDATE clientes SET saltado_hoy = 0, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
            conn.commit()
        cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
        cliente = cursor.fetchone()
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s", (cliente_id,))
        pagos = cursor.fetchall()
    pagado_total = sum(p["valor_pagado"] for p in pagos)
    saldo_restante = max(0.0, cliente["monto_total"] - pagado_total)
    texto_ws = f"🌐 *AVANTA PAGOS - COMPROBANTE*\\n\\nCliente: *{cliente['nombre']}*\\n🔹 Cuota Cobrada: #{num_cuota}\\n🔹 Valor Pagado: \${monto_ingresado:.2f}\\n🔹 Saldo Pendiente: \${saldo_restante:.2f}\\n¡Muchas gracias por su puntualidad!"
    recibo = {
        "cliente": cliente["nombre"], "cuota": num_cuota,
        "monto": monto_ingresado, "saldo": saldo_restante,
        "telefono": str(cliente["telefono"]).replace("(", "").replace(")", "").replace("'", "").strip(),
        "mensaje_ws": urllib.parse.quote(texto_ws),
    }
    conn.close()
    return jsonify({"status": "ok", "recibo": recibo})

@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("UPDATE clientes SET saltado_hoy = saltado_hoy + 1, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/eliminar_cliente/<int:id>")
def api_eliminar_cliente(id):
    conn = get_db()
    with conn.cursor() as cursor: cursor.execute("DELETE FROM clientes WHERE id = %s", (id,))
    conn.commit()
    conn.close()
    return redirect("/menu/clientes")

@app.route("/mover/int:cliente_id/string:direccion")
def mover(cliente_id, direccion):
    conn = get_db()
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
        conn.close()
    return redirect("/")

if name == "main": # type: ignore
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)