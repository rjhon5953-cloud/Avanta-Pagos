import base64
import os
import psycopg2
from psycopg2.extras import DictCursor
import urllib.parse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, send_file

app = Flask(__name__)

# 🔑 Conexión definitiva a tu base de datos de Neon en la nube
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
                    identificacion TEXT,
                    monto REAL NOT NULL,
                    interes_porcentaje REAL NOT NULL DEFAULT 20,
                    monto_total REAL NOT NULL DEFAULT 0,
                    cuotas INTEGER NOT NULL,
                    frecuencia TEXT NOT NULL,
                    valor_cuota REAL NOT NULL,
                    fecha_inicio TEXT NOT NULL,
                    saltado_hoy INTEGER NOT NULL DEFAULT 0,
                    fecha_gestion TEXT DEFAULT '',
                    latitud TEXT DEFAULT '',
                    longitud TEXT DEFAULT ''
                )
                """
            )
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
                CREATE TABLE IF NOT EXISTS gastos (
                    id SERIAL PRIMARY KEY,
                    categoria TEXT NOT NULL DEFAULT 'Otros',
                    concepto TEXT NOT NULL,
                    monto REAL NOT NULL,
                    fecha TEXT NOT NULL,
                    comprobante TEXT DEFAULT ''
                )
                """
            )
        conn.commit()

init_db()
def obtener_clientes_completos():
    conn = get_db()
    todos_clientes = []
    hoy_str = date.today().isoformat()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes ORDER BY orden ASC, id DESC")
        clientes = cursor.fetchall()
        if not clientes:
            conn.close()
            return []
        cliente_ids = [c["id"] for c in clientes]
        placeholders = ",".join("%s" for _ in cliente_ids)
        query_pagos = f"SELECT * FROM pagos WHERE cliente_id IN ({placeholders}) ORDER BY numero ASC"
        cursor.execute(query_pagos, tuple(cliente_ids))
        pagos = cursor.fetchall()

    pagos_por_cliente = defaultdict(list)
    for p in pagos:
        pagos_por_cliente[p["cliente_id"]].append(dict(p))

    for c in clientes:
        cliente_dict = dict(c)
        cliente_dict["pagos"] = pagos_por_cliente.get(c["id"], [])
        
        # Limpieza absoluta de strings de teléfonos devueltos por PostgreSQL
        tel_raw = str(cliente_dict.get("telefono") or '')
        cliente_dict["telefono"] = tel_raw.replace("(", "").replace(")", "").replace("'", "").replace(",", "").replace('"', '').strip()

        atrasadas = 0
        for p in cliente_dict["pagos"]:
            if not p.get("pagado") and p.get("fecha", "") <= hoy_str:
                atrasadas += 1
        
        pago_hoy = any(p.get("fecha_pago_real") == hoy_str for p in cliente_dict["pagos"])
        cliente_dict["cuotas_atrasadas"] = atrasadas
        cliente_dict["gestionado_hoy"] = (cliente_dict.get("fecha_gestion") == hoy_str) or pago_hoy
        todos_clientes.append(cliente_dict)

    conn.close()
    return todos_clientes

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

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente, ID o negocio..." onkeyup="filtrarClientes()">

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
                                <span style="font-size:14px; font-weight:800; color:#0f2b5c;">{{ c.nombre }}</span>
                                {% if es_mora %}
                                    <span class="badge-mora">MORA ({{ c.cuotas_atrasadas }})</span>
                                {% else %}
                                    <span class="badge-al-dia">AL DÍA</span>
                                {% endif %}
                            </div>
                            <div style="font-size:11px; color:#6b7280; margin-top:2px;">
                                ID/Ref: <b>{{ c.identificacion or 'N/A' }}</b> | Tel: <b>{{ c.telefono or 'N/A' }}</b>
                            </div>
                        </div>
                    </div>

                    <div style="text-align:right;">
                        <div style="font-size:10px; color:#6b7280; font-weight:bold;">Cuota:</div>
                        <div style="font-size:16px; font-weight:800; color:#0f2b5c;">${{ "%.2f"|format(c.valor_cuota) }}</div>
                    </div>
                </div>

                <div style="background:#f8fafc; padding:8px; border-radius:6px; margin:8px 0; display:flex; justify-content:space-between; font-size:11px;">
                    <span>Capital: <b>${{ "%.2f"|format(c.monto) }}</b></span>
                    <span>Total: <b>${{ "%.2f"|format(c.monto_total) }}</b></span>
                    <span>Saldo: <b style="color:#ef4444;">${{ "%.2f"|format(saldo) }}</b></span>
                </div>

                <div style="display:flex; gap:6px; justify-content:space-between; align-items:center;">
                {% if c.telefono and cuota_pendiente %}
                        {% set msg_recordatorio = "Hola " ~ c.nombre ~ ", recordatorio de pago. Tu cuota pendiente es de $" ~ "%.2f"|format(cuota_pendiente.valor - cuota_pendiente.valor_pagado) ~ ". Saldo pendiente: $" ~ "%.2f"|format(saldo) ~ ". ¡Gracias!" %}
                        <!-- 🔥 CORRECCIÓN: Enlace directo a la API nativa de la App de WhatsApp -->
                        <a href="https://whatsapp.com{{ c.telefono }}&text={{ msg_recordatorio | urlencode }}" target="_blank" style="text-decoration:none; font-size:11px; color:#0284c7; font-weight:bold;">📩 Recordatorio</a>
                    {% else %}
                        <div></div>
                    {% endif %}

                    <div style="display:flex; gap:6px;">
                        {% if cuota_pendiente %}
                            <button class="btn-accion btn-pagar" onclick="ejecutarPago({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})">💵 Recaudar</button>
                            <button class="btn-accion btn-abono" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})">✏️ Abono</button>
                            <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({{ c.id }})">❌ Saltar</button>
                        {% endif %}
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
{% elif vista == 'nuevo' or vista == 'renovar' %}
    <h3 style="margin-top:0; color:#0f2b5c;">👤 Registro de Crédito / Venta</h3>
    <div class="card" style="padding:16px;">
        <form action="/guardar" method="POST">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Nombre del Cliente</label>
            <input type="text" name="nombre" required placeholder="Ej: Juan Pérez">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Teléfono WhatsApp</label>
            <input type="text" name="telefono" placeholder="Ej: 593991234567">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Identificación / Centro de Negocio</label>
            <input type="text" name="identificacion" placeholder="Cédula o ID comercial">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Monto Financiado ($)</label>
            <input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Porcentaje de Interés (%)</label>
            <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Número de Cuotas</label>
            <input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            <div style="background:#f0f9ff; border:1px solid #bae6fd; padding:10px; border-radius:6px; margin-bottom:10px;">
                <div style="font-size:11px; color:#0369a1; font-weight:bold;">Simulación de Cuota:</div>
                <div style="font-size:16px; font-weight:800; color:#0f2b5c;" id="simulacionText">$0.00 / cuota</div>
            </div>
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Frecuencia de Recaudo</label>
            <select name="frecuencia">
                <option value="Diaria">Diaria</option>
                <option value="Semanal">Semanal</option>
                <option value="Quincenal">Quincenal</option>
                <option value="Mensual">Mensual</option>
            </select>
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Fecha Primer Pago</label>
            <input type="date" name="fecha_inicio" value="{{ hoy_str }}" required>
            <button type="submit" class="btn-primary">💾 Confirmar y Guardar</button>
        </form>
    </div>

{% elif vista == 'cierre' %}
    <h3 style="margin-top:0; color:#0f2b5c;">💼 Cierre de Caja y Recaudo Diario</h3>
    <div class="card" style="padding:16px;">
        <div style="font-size:12px; color:#6b7280;">Fecha de Auditoría: <b>{{ fecha_hoy }}</b></div>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:12px 0;">
            <div style="background:#ecfdf5; padding:10px; border-radius:6px; border:1px solid #a7f3d0;">
                <div style="font-size:10px; color:#065f46; font-weight:bold;">+ Total Recaudado</div>
                <div style="font-size:18px; font-weight:800; color:#047857;">${{ "%.2f"|format(total_cobrado_hoy) }}</div>
            </div>
            <div style="background:#fef2f2; padding:10px; border-radius:6px; border:1px solid #fecaca;">
                <div style="font-size:10px; color:#991b1b; font-weight:bold;">- Total Gastos</div>
                <div style="font-size:18px; font-weight:800; color:#b91c1c;">${{ "%.2f"|format(total_gastos_hoy) }}</div>
            </div>
        </div>
        <div style="background:#f0f9ff; border:1px solid #bae6fd; padding:12px; border-radius:8px; text-align:center; margin-bottom:12px;">
            <div style="font-size:11px; color:#0369a1; font-weight:bold;">Efectivo Neto a Entregar (Caja Final)</div>
            <div style="font-size:24px; font-weight:800; color:#0f2b5c;">${{ "%.2f"|format(total_cobrado_hoy - total_gastos_hoy) }}</div>
        </div>
        <button onclick="alert('Cierre de caja generado correctamente')" class="btn-primary" style="background:#0f2b5c;">🔒 Finalizar y Cerrar Caja Hoy</button>
    </div>

{% elif vista == 'gastos' %}
    <h3 style="margin-top:0; color:#0f2b5c;">💸 Registro de Egresos y Gastos</h3>
    <div class="card" style="padding:16px;">
        <form action="/guardar_gasto" method="POST">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Categoría del Egreso</label>
            <select name="categoria" required>
                <option value="Combustible">⛽ Combustible / Gasolina</option>
                <option value="Alimentación">🍔 Alimentación / Almuerzo</option>
                <option value="Mantenimiento">🛠️ Mantenimiento Vehículo</option>
                <option value="Viáticos">🎒 Viáticos de Ruta</option>
                <option value="Sueldos">💵 Sueldos / Pagos</option>
                <option value="Otros" selected>📦 Otros Egresos</option>
            </select>
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Descripción / Detalle</label>
            <input type="text" name="concepto" placeholder="Ej: Compra de papelería">
            <label style="font-size:11px; font-weight:bold; color:#4b5563;">Monto del Egreso ($)</label>
            <input type="number" step="any" name="monto" placeholder="Ej: 15.00" required>
            <button type="submit" class="btn-primary" style="background:#ef4444; margin-top:8px;">➕ Registrar Egreso</button>
        </form>
    </div>

    <div style="background:#fef2f2; border:1px solid #fecaca; padding:12px; border-radius:8px; display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <span style="font-size:12px; font-weight:800; color:#991b1b;">Total Egresos Registrados Hoy:</span>
        <span style="font-size:18px; font-weight:800; color:#dc2626;">-${{ "%.2f"|format(total_gastos_hoy) }}</span>
    </div>

    <div class="card" style="padding:16px;">
        <h4 style="margin-top:0; font-size:13px; color:#0f2b5c;">📋 Historial de Egresos de Hoy</h4>
        {% if gastos_list %}
            <div style="overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:12px;">
                    <thead>
                        <tr style="background:#f1f5f9; text-align:left; color:#475569;">
                            <th style="padding:8px;">Categoría</th>
                            <th style="padding:8px;">Detalle</th>
                            <th style="padding:8px; text-align:right;">Monto</th>
                            <th style="padding:8px; text-align:center;">Borrar</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for g in gastos_list %}
                        <tr style="border-bottom:1px solid #e2e8f0;">
                            <td style="padding:8px;"><span style="background:#f3f4f6; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;">{{ g.categoria }}</span></td>
                            <td style="padding:8px;"><b>{{ g.concepto or 'Gasto registrado' }}</b></td>
                            <td style="padding:8px; text-align:right; color:#dc2626; font-weight:bold;">-${{ "%.2f"|format(g.monto) }}</td>
                            <td style="padding:8px; text-align:center;">
                                <a href="/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar egreso?')" style="text-decoration:none; color:#ef4444; font-weight:bold; background:#fee2e2; padding:3px 6px; border-radius:4px;">🗑️ Borrar</a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        {% else %}
            <p style="font-size:12px; color:#6b7280; text-align:center; margin:10px 0;">No hay egresos registrados hoy.</p>
        {% endif %}
    </div>

{% elif vista == 'resumen' %}
    <h3 style="margin-top:0; color:#0f2b5c;">📊 Reportes y Copia de Seguridad</h3>
    <div class="card" style="padding:16px;">
        <p style="margin:6px 0;"><b>👥 Clientes en Sistema:</b> {{ clientes | length }}</p>
        <p style="margin:6px 0;"><b>🏙️ Capital Total Prestado:</b> ${{ "%.2f"|format(total_capital) }}</p>
        <p style="margin:6px 0;"><b>💰 Cartera Total con Interés:</b> ${{ "%.2f"|format(total_creditos) }}</p>
        <p style="margin:6px 0;"><b>✅ Total Recaudado:</b> <span style="color:#10b981; font-weight:bold;">${{ "%.2f"|format(total_cobrado) }}</span></p>
        <p style="margin:6px 0;"><b>📌 Pendiente de Recaudo:</b> <span style="color:#ef4444; font-weight:bold;">${{ "%.2f"|format(total_creditos - total_cobrado) }}</span></p>
    </div>
{% endif %}
"""
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AVANTA PAGOS</title>
    <link href="https://googleapis.com" rel="stylesheet">
    <style>
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; }
        body { background: #f1f5f9; margin: 0; padding: 10px; color: #0f172a; }
        .header { background: #0f2b5c; padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; color: white; }
        .btn-menu { background: rgba(255,255,255,0.15); color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: 700; font-size: 13px; cursor: pointer; }
        .brand-title { font-size: 14px; font-weight: 800; color: #ffffff; }
        .badge-status { background: #00a8cc; color: white; padding: 3px 8px; border-radius: 12px; font-size: 10px; font-weight: 800; }
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.6); z-index: 300; }
        .drawer-overlay.active { display: block; }
        .drawer { position: fixed; top: 0; left: -280px; width: 280px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s ease; display: flex; flex-direction: column; }
        .drawer.active { left: 0; }
        .drawer-header { background: #0f2b5c; color: white; padding: 18px 16px; }
        .drawer-menu { list-style: none; padding: 0; margin: 0; }
        .drawer-menu li a { display: flex; align-items: center; gap: 10px; padding: 13px 18px; color: #334155; text-decoration: none; font-weight: 700; font-size: 13px; border-bottom: 1px solid #f1f5f9; }
        .card { background: #ffffff; padding: 12px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #e2e8f0; }
        .btn-filtro { background: #e2e8f0; border: none; padding: 6px 12px; border-radius: 20px; font-size: 11px; font-weight: bold; color: #475569; cursor: pointer; }
        .btn-filtro.active { background: #0f2b5c; color: white; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .search-box { width: 100%; padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 6px; margin-bottom: 10px; font-size: 13px; outline: none; background: white; }
        .btn-accion { border: none; padding: 6px 10px; border-radius: 6px; font-weight: 800; font-size: 11px; cursor: pointer; color: white; }
        .btn-pagar { background: #10b981; } .btn-abono { background: #00a8cc; } .btn-nopagar { background: #ef4444; }
        input, select, button { width: 100%; padding: 10px; margin: 4px 0 10px 0; border: 1px solid #cbd5e1; border-radius: 6px; }
        button.btn-primary { color: white; font-weight: 800; border: none; background: #00a8cc; cursor: pointer; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.6); z-index: 400; justify-content: center; align-items: center; }
        .modal-content { background: white; border-radius: 10px; padding: 20px; width: 100%; max-width: 360px; text-align: center; }
        .btn-ws { background: #25d366; color: white; text-decoration: none; display: block; padding: 10px; border-radius: 6px; font-weight: bold; margin-top: 8px; }
    </style>
</head>
"""
HTML_TEMPLATE += """
<body>
<div class="header">
    <button class="btn-menu" onclick="toggleDrawer()">☰ Menú</button>
    <div class="brand-title">🌐 AVANTA PAGOS</div>
    <span class="badge-status">EN LÍNEA</span>
</div>
<div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
<div class="drawer" id="drawer">
    <div class="drawer-header">
        <div style="font-weight:800; font-size:15px;">🌐 AVANTA PAGOS</div>
        <div style="font-size:11px; opacity:0.8; margin-top:2px;">Centro de Negocio (CN-01)</div>
    </div>
    <ul class="drawer-menu">
        <li><a href="#" onclick="navegarRuta('/')">📍 Ruta de Cobranza</a></li>
        <li><a href="#" onclick="navegarRuta('/nuevo')">👤 Nuevo Crédito / Venta</a></li>
        <li><a href="#" onclick="navegarRuta('/gastos')">💸 Registrar Gastos</a></li>
        <li><a href="#" onclick="navegarRuta('/cierre')">💼 Cierre y Recaudo Diario</a></li>
        <li><a href="#" onclick="navegarRuta('/resumen')">📊 Reporte y Backup</a></li>
    </ul>
</div>
<div id="mainContent">{{ contenido_html | safe }}</div>

<div class="modal" id="modalWs">
    <div class="modal-content">
        <h3 style="margin-top:0; color:#10b981;">✅ Recaudo Exitoso</h3>
        <p id="modalMsg">Se ha procesado el pago correctamente.</p>
        <a href="#" id="modalWsBtn" target="_blank" class="card btn-ws">📲 Enviar Comprobante WhatsApp</a>
        <button onclick="cerrarModal('modalWs')" style="margin-top:10px; background:#e2e8f0; border:none; color:#0f172a; padding:10px; border-radius:6px; width:100%;">Cerrar</button>
    </div>
</div>

<div class="modal" id="modalAbono">
    <div class="modal-content">
        <h3>✏️ Registrar Abono Parcial</h3>
        <p style="font-size:12px; color:#6b7280;">Monto restante de la cuota: <b id="abonoPendienteText">\$0.00</b></p>
        <input type="hidden" id="abonoClienteId"><input type="hidden" id="abonoNumCuota">
        <input type="number" step="any" id="abonoMontoInput" placeholder="Ingresa valor del abono (\$)">
        <button onclick="confirmarAbono()" class="btn-primary">💾 Guardar Abono</button>
        <button onclick="cerrarModal('modalAbono')" style="margin-top:4px; background:#e2e8f0; border:none; color:#0f172a;">Cancelar</button>
    </div>
</div>

<script>
function toggleDrawer() {
    document.getElementById('drawer').classList.toggle('active');
    document.getElementById('drawerOverlay').classList.toggle('active');
}
function navegarRuta(url) {
    const overlay = document.getElementById('drawerOverlay');
    if (overlay.classList.contains('active')) toggleDrawer();
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(res => res.text())
        .then(html => { document.getElementById('mainContent').innerHTML = html; });
}
function filtrarClientes() {
    const query = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.cliente-card').forEach(card => {
        const nombre = card.getAttribute('data-nombre');
        card.style.display = nombre.includes(query) ? "block" : "none";
    });
}
function filtrarEstado(tipo) {
    document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('active'));
    document.getElementById(`f-${tipo}`).classList.add('active');
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
        document.getElementById('simulacionText').innerText = `$${(total / cuotas).toFixed(2)} / cuota (Total: $${total.toFixed(2)})`;
    } else {
        document.getElementById('simulacionText').innerText = "\$0.00 / cuota";
    }
}
function ejecutarPago(clienteId, numCuota, monto) { procesarPagoAPI(clienteId, numCuota, monto); }
function abrirModalAbono(clienteId, numCuota, pendiente) {
    document.getElementById('abonoClienteId').value = clienteId;
    document.getElementById('abonoNumCuota').value = numCuota;
    document.getElementById('abonoPendienteText').innerText = `$${pendiente.toFixed(2)}`;
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
            fetch(`/api/marcar_pago/${clienteId}/${numCuota}?monto=${monto}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'ok') {
                        const card = document.getElementById(`cliente-card-${clienteId}`);
                        if (card) card.style.display = 'none';
                        if (data.recibo && data.recibo.telefono) {
                            document.getElementById('modalMsg').innerText = `Recaudo de $${data.recibo.monto.toFixed(2)} registrado para ${data.recibo.cliente}.`;
                            
                            // 🔥 CORRECCIÓN QUIRÚRGICA: Forzamos el enlace directo a la App nativa sin abrir el navegador web
                            const numPuro = data.recibo.telefono.toString().replace(/[^0-9]/g, '').trim();
                            document.getElementById('modalWsBtn').href = 'https://whatsapp.com' + numPuro + '&text=' + data.recibo.mensaje_ws;
                            
                            document.getElementById('modalWs').style.display = 'flex';
                        }
                    }
                });
}
function ejecutarNoPago(clienteId) {
    fetch(`/api/marcar_no_pago/${clienteId}`).then(res => res.json()).then(data => {
        if (data.status === 'ok') document.getElementById(`cliente-card-${clienteId}`).style.display = 'none';
    });
}
function cerrarModal(id) { document.getElementById(id).style.display = 'none'; }
</script>
</body>
</html>
"""

@app.route("/")
def lista():
    todos_clientes = obtener_clientes_completos()
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
        res_cobrado = cursor.fetchone()
        # CORRECCIÓN: Extraemos el número en la posición 0
        total_cobrado_hoy = res_cobrado[0] if res_cobrado and res_cobrado[0] is not None else 0.0

        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        # CORRECCIÓN: Extraemos el número en la posición 0
        total_gastos_hoy = res_gastos[0] if res_gastos and res_gastos[0] is not None else 0.0
    conn.close()

    clientes_filtrados = [
        c for c in todos_clientes
        if any(not p["pagado"] for p in c["pagos"]) and not c.get("gestionado_hoy", False)
    ]
    capital_en_calle = sum(max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"])) for c in todos_clientes)

    contexto = dict(vista="lista", clientes=clientes_filtrados, total_cobrado_hoy=total_cobrado_hoy, total_gastos_hoy=total_gastos_hoy, capital_en_calle=capital_en_calle)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

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

    texto_ws = f"🌐 *AVANTA PAGOS - RECAUDO*\\n\\nCliente: *{cliente['nombre']}*\\n🔹 Cuota: {num_cuota}/{len(pagos)}\\n🔹 Recaudado: ${monto_ingresado:.2f}\\n🔹 Saldo Pendiente: ${saldo_restante:.2f}"
    
    tel_sucio = str(cliente['telefono'] if 'telefono' in cliente else cliente)
    tel_real = tel_sucio.replace("(", "").replace(")", "").replace("'", "").replace(",", "").replace('"', '').strip()

    recibo = {
        "cliente": cliente["nombre"],
        "cuota": num_cuota,
        "monto": monto_ingresado,
        "saldo": saldo_restante,
        "telefono": tel_real,
        "mensaje_ws": urllib.parse.quote(texto_ws),
    }
    conn.close()
    return jsonify({"status": "ok", "recibo": recibo})

@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("UPDATE clientes SET saltado_hoy = 1, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})
@app.route("/mover/<int:cliente_id>/<string:direccion>")
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

@app.route("/nuevo")
def nuevo_cliente():
    hoy_str = date.today().isoformat()
    contexto = dict(vista="nuevo", hoy_str=hoy_str, cliente=None)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/guardar", methods=["POST"])
def guardar():
    try:
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").replace("+", "").replace(" ", "").strip()
        identificacion = request.form.get("identificacion", "").strip()
        monto = float(request.form.get("monto", "0").replace(",", "."))
        interes_porcentaje = float(request.form.get("interes_porcentaje", "20").replace(",", "."))
        cuotas = int(request.form.get("cuotas", "0"))
        frecuencia = request.form.get("frecuencia")
        fecha_inicio = date.fromisoformat(request.form.get("fecha_inicio"))

        monto_total = round(monto * (1 + (interes_porcentaje / 100)), 2)
        valor_base = round(monto_total / cuotas, 2)
        acumulado = 0.0

        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes")
            res_max = cursor.fetchone()
            max_orden = res_max if res_max and res_max is not None else 0
            cursor.execute(
                """
                INSERT INTO clientes (orden, nombre, telefono, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, saltado_hoy, fecha_gestion)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, '')
                RETURNING id
                """,
                (max_orden + 1, nombre, telefono, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base, fecha_inicio.isoformat()),
            )
            cliente_id = cursor.fetchone()

            for num in range(1, cuotas + 1):
                valor_cuota = round(monto_total - acumulado, 2) if num == cuotas else valor_base
                acumulado += valor_cuota
                f_cuota = calcular_fecha(fecha_inicio, num - 1, frecuencia)
                cursor.execute("INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real) VALUES (%s, %s, %s, %s, 0, 0, '')", (cliente_id, num, f_cuota.isoformat(), valor_cuota))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error en guardar: {e}")
    return redirect("/")

@app.route("/gastos")
def vista_gastos():
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM gastos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
        gastos_list = cursor.fetchall()
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos if res_gastos and res_gastos is not None else 0.0
    conn.close()
    contexto = dict(vista="gastos", gastos_list=gastos_list, total_gastos_hoy=total_gastos_hoy)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/guardar_gasto", methods=["POST"])
def guardar_gasto():
    try:
        categoria = request.form.get("categoria", "Otros")
        concepto = request.form.get("concepto", "").strip()
        monto = float(request.form.get("monto", "0").replace(",", "."))
        hoy_str = date.today().isoformat()
        if not concepto:
            concepto = f"Gasto de {categoria}"
        if monto > 0:
            conn = get_db()
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO gastos (categoria, concepto, monto, fecha, comprobante) VALUES (%s, %s, %s, %s, '')", (categoria, concepto, monto, hoy_str))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"❌ Gasto error: {e}")
    return redirect("/gastos")

@app.route("/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM gastos WHERE id = %s", (gasto_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Eliminar error: {e}")
    return redirect("/gastos")

@app.route('/cierre')
def cierre_diario():
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
        res_cobrado = cursor.fetchone()
        total_cobrado_hoy = res_cobrado[0] if res_cobrado and res_cobrado[0] is not None else 0.0
        
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos[0] if res_gastos and res_gastos[0] is not None else 0.0
    conn.close()
    contexto = dict(vista='cierre', total_cobrado_hoy=total_cobrado_hoy, total_gastos_hoy=total_gastos_hoy, fecha_hoy=hoy_str)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/resumen")
def resumen():
    clientes = obtener_clientes_completos()
    total_capital = sum(c["monto"] for c in clientes)
    total_creditos = sum(c["monto_total"] for c in clientes)
    total_cobrado = sum(p["valor_pagado"] for c in clientes for p in c["pagos"])
    contexto = dict(vista="resumen", clientes=clientes, total_capital=total_capital, total_creditos=total_creditos, total_cobrado=total_cobrado)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto))

@app.route("/respaldo")
def respaldo():
    return redirect("/resumen")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
