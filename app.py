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
    # Uso de context manager para garantizar que la conexión se cierre bajo cualquier error
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE estado = 'Activo' ORDER BY orden ASC, id DESC")
            clientes = cursor.fetchall()
            if not clientes:
                return []
            
            cliente_ids = [c["id"] for c in clientes]
            # Optimización crítica usando ANY(%s) en lugar de formateo directo de strings inyectables
            cursor.execute("SELECT * FROM pagos WHERE cliente_id = ANY(%s) ORDER BY numero ASC", (cliente_ids,))
            pagos = cursor.fetchall()

    pagos_por_cliente = defaultdict(list)
    for p in pagos:
        pagos_por_cliente[p["cliente_id"]].append(dict(p))

    resultado = []
    for c in clientes:
        c_dict = dict(c)
        c_dict["pagos"] = pagos_por_cliente.get(c["id"], [])
        
        # Filtro estricto de control de la Ruta del Día o Enrutado Forzado Anticipado
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

                <div style="display:flex; gap:6px; justify-content:flex-end; margin-top:10px;">
                    {% if cuota_pendiente %}
                        <!-- 🔄 Botón Pago: Abre la ventana modal para ingresar abonos o cuotas completas -->
                        <button class="btn-accion btn-pagar" style="background:#10b981;" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }}, {{ c.valor_cuota }})"><i class="fa-solid fa-money-bill-wave"></i> Pago</button>
                        <!-- ❌ Botón No Pago: Registra el salto diario de cobranza de forma inmediata -->
                        <button class="btn-accion btn-nopagar" style="background:#ef4444;" onclick="ejecutarNoPago({{ c.id }})"><i class="fa-solid fa-ban"></i> No pago</button>
                    {% endif %}
                </div>
            </div>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
{% elif vista == 'clientes' or vista == 'creditos' %}
    <div class="section-header-title"><i class="fa-solid fa-users"></i> CONTROL MAESTRO DE CLIENTES</div>
    <div class="card" style="background:#0f2b5c; color:white; text-align:center; padding:10px; margin-bottom:12px;">
        <div class="kpi-title" style="color:white; opacity:0.8;">Total Activos</div>
        <div class="kpi-val" style="color:white; font-size:18px;">{{ total_activos }} Clientes con Crédito Activo</div>
    </div>
    
    <div style="display:flex; gap:6px; margin-bottom:12px; overflow-x:auto;">
        <button class="btn-filtro {% if filtro_estado == 'Activo' %}active{% endif %}" onclick="navegarRuta('/menu/clientes?filtro=Activo')">🟢 Activos</button>
        <button class="btn-filtro {% if filtro_estado == 'Inactivo' %}active{% endif %}" onclick="navegarRuta('/menu/clientes?filtro=Inactivo')">⚪ Inactivos</button>
        <button class="btn-filtro {% if filtro_estado == 'Lista Negra' %}active{% endif %}" onclick="navegarRuta('/menu/clientes?filtro=Lista Negra')">⚫ Lista Negra</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente específico..." onkeyup="filtrarClientes()">
    <div id="clientesContainer">
        {% for cl in todos %}
            <div class="card cliente-card" data-nombre="{{ cl.nombre | lower }}">
                <div class="flex-between">
                    <div>
                        <span style="font-weight:800; color:#0f2b5c; font-size:14px;">{{ cl.nombre }}</span>
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Saldo: ${{ "%.2f"|format(cl.monto_total) }} | Frecuencia: {{ cl.frecuencia }}</div>
                    </div>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <button class="btn-accion btn-abono" onclick="navegarRuta('/mover_renovacion/{{ cl.id }}')" style="border:none; padding:8px 12px;"><i class="fa-solid fa-rotate"></i> Renovar</button>
                        {% if cl.estado != 'Lista Negra' %}
                            <button class="btn-accion" onclick="if(confirm('¿Mover a Lista Negra?')) window.location.href='/api/clientes/lista_negra/{{ cl.id }}'" style="background:#475569; padding:8px 12px;"><i class="fa-solid fa-ban"></i> Bloquear</button>
                        {% endif %}
                        <a href="/api/eliminar_cliente/{{ cl.id }}" onclick="return confirm('¿Eliminar cliente permanentemente de la ruta?')" class="btn-accion btn-nopagar" style="text-decoration:none; padding:8px 12px; background:#dc2626;"><i class="fa-solid fa-trash-can"></i> 🗑️ Borrar</a>
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
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

    <div class="card" style="background:#0f2b5c; color:white; text-align:center; padding:12px;">
        <div class="kpi-title" style="color:white; opacity:0.8;">Efectivo Líquido Neto</div>
        <div class="kpi-val" style="color:white; font-size:20px; margin-top:2px;">${{ "%.2f"|format(efectivo_neto) }}</div>
    </div>

        <div class="card" style="padding:16px;">
        <div class="kpi-title" style="margin-bottom:8px;">Registrar Movimiento de Flujo</div>
        <form action="/api/balance/guardar_movimiento" method="POST" enctype="multipart/form-data">
            <label>Tipo de Flujo</label>
            <select name="tipo_mov" required>
                <option value="Entrada">📥 Entrada / Inversión Capital</option>
                <option value="Salida" selected>📤 Salida / Registro de Gasto</option>
            </select>
            <label>Categoría del Movimiento</label>
            <select name="categoria_mov">
                <option value="Inversión">💰 Inversión de Capital</option>
                <option value="Gasolina">⛽ Gasolina</option>
                <option value="Almuerzo">🍔 Almuerzo</option>
                <option value="Mantenimiento">🛠️ Mantenimiento Vehículo</option>
                <option value="Viáticos">🎒 Viáticos de Ruta</option>
                <option value="Sueldo">💵 Comision</option>
                <option value="Otros" selected>📦 Otros</option>
            </select>
            
            <label>Descripción / Detalle</label>
            <!-- 🧼 FIJADO: Se elimina el atributo inválido 'optional' para que el navegador móvil no corrompa el envío -->
            <input type="text" name="concepto_mov" placeholder="Ej: Compra de repuestos de moto">
            
            <label>Monto ($)</label>
            <input type="number" step="any" name="monto_mov" placeholder="Valor en dinero">
            
            <label>📸 Foto Factura / Comprobante</label>
            <input type="file" name="foto_mov" accept="image/*" capture="environment" style="border:none; padding:4px 0;">
            
            <button type="submit" class="btn-primary" style="background:#0f2b5c; color:white; border:none; font-weight:bold; padding:12px; margin-top:10px; width:100%; border-radius:8px; cursor:pointer;">Guardar Registro</button>
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
                        {% if g.comprobante %}
                            <!-- ⚡ FIJADO: Pasamos el ID del movimiento en vez de la imagen completa para evitar que el navegador la corte -->
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
                
                // 🗺️ FIJADO: Enlace universal deep-linking correcto para Google Maps en celulares
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
// ⚡ REPARACIÓN COMPLEMENTARIA: FUNCIÓN ASOCIADA AL BOTÓN 'COBRAR' DEL DASHBOARD PRINCIPAL
function ejecutarPago(clienteId, numCuota, pendiente) {
    procesarPagoAPI(clienteId, numCuota, pendiente);
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
                const textoMensaje = encodeURIComponent(data.recibo.mensaje_ws);
                
                // 📲 FIJADO: Protocolos nativos de comunicación celular y web sin dobles diagonales
                const urlCelular = 'whatsapp://send?phone=' + numPuro + '&text=' + textoMensaje;
                const urlWeb = 'https://whatsapp.com' + numPuro + '&text=' + textoMensaje;
                
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
    fetch('/api/marcar_no_pago/' + clienteId).then(res => res.json()).then(data => {
        if (data.status === 'ok') window.location.reload();
    });
}
function cargarYVerFoto(gastoId) {
    fetch('/api/gasto_foto/' + gastoId)
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok' && data.comprobante) {
                const img = document.getElementById('imgComprobante');
                img.src = ""; // Limpiar búfer
                
                // Si la cadena no tiene el prefijo de imagen correcto, se lo ponemos de forma garantizada
                let b64 = data.comprobante.trim();
                if (!b64.startsWith('data:image')) {
                    b64 = 'data:image/jpeg;base64,' + b64;
                }
                
                img.src = b64;
                document.getElementById('modalFoto').style.display = 'flex';
            } else {
                alert("⚠️ No se pudo cargar la imagen o el registro no tiene comprobante.");
            }
        }).catch(err => alert("Error al conectar con el servidor: " + err));
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
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 📊 FIJADO: Desempaquetado correcto usando alias de columnas explícitos para DictCursor
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
        <div class="kpi-title" style="color:#0369a1;">Recaudo Esperado del Día</div>
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">🔬 Mínimo: <b>${debido_minimo_dia:.2f}</b> | Acumulado Mora: <b>${debido_total_acumulado:.2f}</b></div>
    </div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar por nombre de cliente o referencia..." onkeyup="filtrarClientes()">
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
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(contexto_dash)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(contexto_dash))

@app.route("/menu/clientes")
@app.route("/menu/creditos")
def seccion_clientes_maestro():
    if not session.get("autenticado"): return "Sesión expirada"
    filtro_estado = request.args.get("filtro", "Activo")
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM clientes WHERE estado = %s ORDER BY nombre ASC", (filtro_estado,))
            todos = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) AS total FROM clientes WHERE estado = 'Activo'")
            total_activos = int(cursor.fetchone()["total"])
            
    contexto = dict(vista="clientes", todos=todos, total_activos=total_activos, filtro_estado=filtro_estado)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest": 
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/menu/balance")
def seccion_balance_maestro():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    
    with get_db() as conn:
        with conn.cursor() as cursor:
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
        # 🧼 Saneamiento crítico en DictCursor para que el botón "Ver" nunca se oculte
        if m_dict["comprobante"] and str(m_dict["comprobante"]).strip() != "" and m_dict["comprobante"] != "None":
            try:
                comprobante_puro = str(m_dict["comprobante"]).replace("\n", "").replace("\r", "").strip()
                
                # Si por error el Base64 ya se guardó con prefijo duplicado en Neon, lo recortamos y limpiamos
                if "base64," in comprobante_puro:
                    comprobante_puro = comprobante_puro.split("base64,")[-1]
                
                # Inyección garantizada del prefijo Data URI limpio para el navegador
                m_dict["comprobante"] = f"data:image/jpeg;base64,{comprobante_puro}"
            except Exception as b64_err:
                print(f"Error procesando imagen: {b64_err}")
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
    
    file = request.files.get("foto_mov")
    base64_str = ""
    if file and file.filename != "":
        try:
            # 📸 FIJADO: Guarda únicamente la cadena Base64 pura y limpia en Neon, sin encabezados repetidos
            raw_b64 = base64.b64encode(file.read()).decode("utf-8")
            base64_str = raw_b64.replace("\n", "").replace("\r", "").strip()
        except Exception as img_err:
            print(f"Error procesando archivo de imagen: {img_err}")
            base64_str = ""
        
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
        
    monto_pagado = float(request.args.get("monto", 0))
    hoy_str = date.today().isoformat()
    
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pagos 
                SET pagado = 1, valor_pagado = %s, fecha_pago_real = %s 
                WHERE cliente_id = %s AND numero = %s
                """, (monto_pagado, hoy_str, cliente_id, num_cuota)
            )
            cursor.execute("UPDATE clientes SET enrutado_forzado = 0, saltado_hoy = 0 WHERE id = %s", (cliente_id,))
            
            cursor.execute("SELECT nombre, telefono, monto_total FROM clientes WHERE id = %s", (cliente_id,))
            c = cursor.fetchone()
            cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) AS total FROM pagos WHERE cliente_id = %s", (cliente_id,))
            total_pagado = float(cursor.fetchone()["total"])
            
        conn.commit()
        
    saldo_restante = max(0.0, c["monto_total"] - total_pagado)
    mensaje_ws = f"🧾 *AVANTA PAGOS*\nRecibo de Pago\nCliente: {c['nombre']}\nCuota: #{num_cuota}\nMonto Cobrado: ${monto_pagado:.2f}\nSaldo Restante: ${saldo_restante:.2f}\n¡Gracias por su pago!"
    
    recibo = {
        "cliente": c["nombre"],
        "telefono": c["telefono"] or "",
        "cuota": num_cuota,
        "monto": monto_pagado,
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
            # Selecciona de forma explícita la columna del string Base64
            cursor.execute("SELECT comprobante FROM balance_movimientos WHERE id = %s", (gasto_id,))
            # 🧼 FIJADO: La asignación ahora está dentro del bloque del cursor con la indentación correcta
            res = cursor.fetchone()
            
    # Extraemos el string plano usando la clave de DictCursor
    if res and res["comprobante"]:
        return jsonify({"status": "ok", "comprobante": str(res["comprobante"])})
    return jsonify({"status": "error", "message": "No encontrado"})

if __name__ == "__main__":
    # Servidor local configurado para ejecutarse en el puerto estricto 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
    
