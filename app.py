import base64
import os
import psycopg2 # type: ignore
from psycopg2.extras import DictCursor # type: ignore
import urllib.parse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta # type: ignore
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, send_file # type: ignore

app = Flask(__name__)

# 🔑 Enlace real de Neon con contraseña integrada para producción
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

    resultado = []
    for c in clientes:
        cliente_dict = dict(c)
        cliente_dict["pagos"] = pagos_por_cliente.get(c["id"], [])
        
        # Limpieza quirúrgica de tuplas de texto de PostgreSQL para el recordatorio
        tel_raw = str(cliente_dict.get("telefono", ""))
        cliente_dict["telefono"] = tel_raw.replace("(", "").replace(")", "").replace("'", "").replace(",", "").replace('"', '').strip()

        atrasadas = 0
        pago_hoy = False
        for p in cliente_dict["pagos"]:
            if not p.get("pagado") and p.get("fecha", "") <= hoy_str:
                atrasadas += 1
            if p.get("fecha_pago_real") == hoy_str:
                pago_hoy = True
        cliente_dict["cuotas_atrasadas"] = atrasadas
        gestion_hoy = cliente_dict.get("fecha_gestion") == hoy_str
        cliente_dict["gestionado_hoy"] = gestion_hoy or pago_hoy
        resultado.append(cliente_dict)
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
CONTENIDO_HTML = """
{% if vista == 'lista' %}
    <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin-bottom:16px;">
        <div style="background:#ffffff; padding:14px 16px; border-radius:10px; border-left:4px solid #10b981; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <div style="font-size:10px; color:#64748b; font-weight:700; text-transform:uppercase; letter-spacing:0.5px;">Recaudado</div>
            <div style="font-size:18px; font-weight:800; color:#065f46; margin-top:2px;">${{ "%.2f"|format(total_cobrado_hoy) }}</div>
        </div>
        <div style="background:#ffffff; padding:14px 16px; border-radius:10px; border-left:4px solid #ef4444; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <div style="font-size:10px; color:#64748b; font-weight:700; text-transform:uppercase; letter-spacing:0.5px;">Gastos</div>
            <div style="font-size:18px; font-weight:800; color:#991b1b; margin-top:2px;">${{ "%.2f"|format(total_gastos_hoy) }}</div>
        </div>
        <div style="background:#ffffff; padding:14px 16px; border-radius:10px; border-left:4px solid #00a8cc; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <div style="font-size:10px; color:#64748b; font-weight:700; text-transform:uppercase; letter-spacing:0.5px;">En Calle</div>
            <div style="font-size:18px; font-weight:800; color:#0f2b5c; margin-top:2px;">${{ "%.2f"|format(capital_en_calle) }}</div>
        </div>
    </div>

    <div style="display:flex; gap:8px; margin-bottom:14px; overflow-x:auto;">
        <button onclick="filtrarEstado('todos')" class="btn-filtro active" id="f-todos" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">👥 Por Cobrar ({{ clientes | length }})</button>
        <button onclick="filtrarEstado('mora')" class="btn-filtro" id="f-mora" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">⚠️ Mora</button>
        <button onclick="filtrarEstado('aldia')" class="btn-filtro" id="f-aldia" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">✅ Al Día</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente, ID o negocio..." onkeyup="filtrarClientes()">

    <div id="clientesContainer" style="display:flex; flex-direction:column; gap:12px;">
        {% for c in clientes %}
            {% set pagado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo = c.monto_total - pagado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set es_mora = c.cuotas_atrasadas > 0 %}
            <div class="card cliente-card" id="cliente-card-{{ c.id }}" data-mora="{{ 1 if es_mora else 0 }}" data-nombre="{{ c.nombre | lower }}" data-id="{{ c.identificacion or '' }}" style="padding:16px; background:#ffffff; border-radius:8px; border:1px solid #e2e8f0; border-left:4px solid #0f2b5c; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
                <div class="flex-between" style="align-items: flex-start; margin-bottom:12px;">
                    <div style="display:flex; align-items:center; gap:12px;">
                        <div style="display:flex; flex-direction:column; gap:4px;">
                            <a href="/mover/{{ c.id }}/subir" style="text-decoration:none; font-size:11px; background:#f1f5f9; padding:4px 6px; border-radius:4px; color:#475569; font-weight:bold;">⬆️</a>
                            <a href="/mover/{{ c.id }}/bajar" style="text-decoration:none; font-size:11px; background:#f1f5f9; padding:4px 6px; border-radius:4px; color:#475569; font-weight:bold;">⬇️</a>
                        </div>
                        <div>
                            <div style="display:flex; align-items:center; gap:8px;">
                                <span style="font-size:15px; font-weight:700; color:#0f172a; text-transform:capitalize;">{{ c.nombre }}</span>
                                {% if es_mora %}
                                    <span class="badge-mora" style="padding:2px 6px; font-size:10px; font-weight:700;">MORA ({{ c.cuotas_atrasadas }})</span>
                                {% else %}
                                    <span class="badge-al-dia" style="padding:2px 6px; font-size:10px; font-weight:700;">AL DÍA</span>
                                {% endif %}
                            </div>
                            <div style="font-size:12px; color:#64748b; margin-top:4px;">
                                <i class="fa-solid fa-id-card" style="font-size:11px; margin-right:4px;"></i> ID/Ref: <b>{{ c.identificacion or 'N/A' }}</b> | <i class="fa-solid fa-phone" style="font-size:11px; margin-right:4px; margin-left:6px;"></i> Tel: <b>{{ c.telefono or 'N/A' }}</b>
                            </div>
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:11px; color:#64748b; font-weight:600;">Cuota:</div>
                        <div style="font-size:18px; font-weight:800; color:#0f2b5c;">${{ "%.2f"|format(c.valor_cuota) }}</div>
                    </div>
                </div>

                <div style="background:#f8fafc; padding:10px 14px; border-radius:6px; margin:12px 0; display:flex; justify-content:space-between; font-size:12px; color:#334155; border:1px solid #f1f5f9;">
                    <span>Capital: <b>${{ "%.2f"|format(c.monto) }}</b></span>
                    <span>Total: <b>${{ "%.2f"|format(c.monto_total) }}</b></span>
                    <span>Saldo: <b style="color:#ef4444; font-weight:700;">${{ "%.2f"|format(saldo) }}</b></span>
                </div>

                <div style="display:flex; gap:6px; justify-content:space-between; align-items:center; margin-top:8px;">
                    {% if c.telefono and cuota_pendiente %}
                        {% set telefono_limpio = c.telefono | replace('+', '') | replace(' ', '') | trim %}
                        {% set msg_recordatorio = "Hola " ~ c.nombre ~ ", recordatorio de pago. Tu cuota pendiente es de $" ~ "%.2f"|format(cuota_pendiente.valor - cuota_pendiente.valor_pagado) ~ ". Saldo pendiente: $" ~ "%.2f"|format(saldo) ~ ". ¡Gracias!" %}
                        <a href="https://wa.me{{ telefono_limpio }}?text={{ msg_recordatorio | urlencode }}" target="_blank" style="text-decoration:none; font-size:12px; color:#0284c7; font-weight:700; display:flex; align-items:center; gap:4px;">
                            <i class="fa-brands fa-whatsapp" style="font-size:14px;"></i> Recordatorio
                        </a>
                    {% else %}
                        <div></div>
                    {% endif %}

                    <div style="display:flex; gap:8px;">
                        {% if cuota_pendiente %}
                            <button class="btn-accion btn-pagar" onclick="ejecutarPago({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})"><i class="fa-solid fa-money-bill-wave" style="margin-right:4px;"></i> Recaudar</button>
                            <button class="btn-accion btn-abono" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})"><i class="fa-solid fa-pen-to-square" style="margin-right:4px;"></i> Abono</button>
                            <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({{ c.id }})"><i class="fa-solid fa-angles-right" style="margin-right:4px;"></i> Saltar</button>
                        {% endif %}
                    </div>
                </div>
            </div>
        {% else %}
            <p style="text-align:center; color:#64748b; margin-top:24px; font-weight:500;">✅ ¡Excelente! No tienes cobranzas pendientes.</p>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
{% elif vista == 'nuevo' or vista == 'renovar' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">👤 Registro de Crédito / Venta</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0;">
        <form action="/guardar" method="POST">
            <label style="font-size:12px; font-weight:700; color:#475569;">Nombre del Cliente</label>
            <input type="text" name="nombre" required placeholder="Ej: Juan Pérez">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Teléfono WhatsApp</label>
            <input type="text" name="telefono" placeholder="Ej: 593991234567">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Identificación</label>
            <input type="text" name="identificacion" placeholder="Cédula o ID comercial">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Monto Financiado ($)</label>
            <input type="number" step="any" id="calcMonto" name="monto" oninput="calcularCuota()" required>
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Porcentaje de Interés (%)</label>
            <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Número de Cuotas</label>
            <input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Frecuencia de Pago</label>
            <select name="frecuencia" id="calcFrecuencia">
                <option value="Diaria">Diaria (Lunes a Sábado)</option>
                <option value="Semanal">Semanal</option>
                <option value="Quincenal">Quincenal</option>
                <option value="Mensual">Mensual</option>
            </select>
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Fecha de Inicio</label>
            <input type="date" name="fecha_inicio" value="{{ fecha_hoy }}" required>
            <input type="hidden" id="input_latitud" name="latitud" value=""><input type="hidden" id="input_longitud" name="longitud" value="">
            
            <div style="margin:16px 0; font-size:15px; font-weight:800; color:#10b981;" id="simulacionText">$0.00 / cuota</div>
            <button type="submit" class="btn-primary" style="background:#0f172a;">Guardar Crédito</button>
        </form>
    </div>

{% elif vista == 'gastos' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">📉 Registro de Egresos / Gastos</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; margin-bottom:16px;">
        <form action="/guardar_gasto" method="POST">
            <label style="font-size:12px; font-weight:700; color:#475569;">Categoría del Gasto</label>
            <select name="categoria">
                <option value="Combustible">Combustible / Tanqueo</option>
                <option value="Viaticos">Viáticos / Comida</option>
                <option value="Papeleria">Papelería / Impresiones</option>
                <option value="Otros">Otros</option>
            </select>
            <label style="font-size:12px; font-weight:700; color:#475569;">Descripción / Detalle</label>
            <input type="text" name="concepto" placeholder="Ej: Compra de hojas e impresiones">
            <label style="font-size:12px; font-weight:700; color:#475569;">Monto del Egreso ($)</label>
            <input type="number" step="any" name="monto" placeholder="Ej: 10.00" required>
            <button type="submit" class="btn-primary" style="background:#ef4444; margin-top:14px;">Registrar Egreso</button>
        </form>
    </div>

    <div style="background:#fef2f2; border:1px solid #fecaca; padding:14px 16px; border-radius:10px; display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
        <span style="font-size:13px; font-weight:700; color:#991b1b;">Total Egresos Registrados Hoy:</span>
        <span style="font-size:20px; font-weight:800; color:#dc2626;">-${{ "%.2f"|format(total_gastos_hoy) }}</span>
    </div>

    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0;">
        <h4 style="margin-top:0; font-size:14px; color:#0f172a; font-weight:700; margin-bottom:12px;">📋 Historial de Egresos de Hoy</h4>
        {% if gastos_list %}
            <div style="overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead>
                        <tr style="background:#f8fafc; text-align:left; color:#475569; border-bottom:2px solid #e2e8f0;">
                            <th style="padding:10px 8px; font-weight:700;">Categoría</th>
                            <th style="padding:10px 8px; font-weight:700;">Detalle</th>
                            <th style="padding:10px 8px; text-align:right; font-weight:700;">Monto</th>
                            <th style="padding:10px 8px; text-align:center; font-weight:700;">Acciones</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for g in gastos_list %}
                        <tr style="border-bottom:1px solid #f1f5f9;">
                            <td style="padding:10px 8px;"><span style="background:#f1f5f9; color:#475569; padding:3px 8px; border-radius:6px; font-size:11px; font-weight:700;">{{ g.categoria }}</span></td>
                            <td style="padding:10px 8px; color:#1e293b; font-weight:600;">{{ g.concepto or 'Sin detalle' }}</td>
                            <td style="padding:10px 8px; text-align:right; color:#dc2626; font-weight:700;">-${{ "%.2f"|format(g.monto) }}</td>
                            <td style="padding:10px 8px; text-align:center;">
                                <a href="/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar este gasto por completo?')" style="text-decoration:none; color:#ef4444; font-size:11px; font-weight:700; background:#fee2e2; padding:5px 10px; border-radius:4px; display:inline-block;"><i class="fa-solid fa-trash-can"></i> Eliminar</a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        {% else %}
            <p style="font-size:12px; color:#64748b; text-align:center; margin:14px 0;">No hay egresos registrados hoy.</p>
        {% endif %}
    </div>
"""
CONTENIDO_HTML += """
{% elif vista == 'resumen' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">📊 Reportes y Copia de Seguridad</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; line-height:1.8; font-size:14px; color:#334155;">
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>👥 <b>Clientes en Sistema:</b></span> <span style="font-weight:700; color:#1e293b;">{{ clientes | length }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>🏙️ <b>Capital Total Prestado:</b></span> <span style="font-weight:700; color:#1e293b;">${{ "%.2f"|format(total_capital) }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>💰 <b>Cartera Total con Interés:</b></span> <span style="font-weight:700; color:#0f2b5c;">${{ "%.2f"|format(total_creditos) }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>✅ <b>Total Recaudado:</b></span> <span style="color:#10b981; font-weight:700;">${{ "%.2f"|format(total_cobrado) }}</span></div>
        <div style="display:flex; justify-content:space-between; padding:10px 0;"><span>📌 <b>Pendiente de Recaudo:</b></span> <span style="color:#ef4444; font-weight:700;">${{ "%.2f"|format(total_creditos - total_cobrado) }}</span></div>
    </div>

{% elif vista == 'cierre' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">💼 Cierre de Caja Diario</h3>
    <div class="card" style="padding:24px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0;">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px;">
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:16px; border-radius:8px;">
                <div style="font-size:11px; color:#15803d; font-weight:700;">+ Total Recaudado</div>
                <div style="font-size:24px; font-weight:800; color:#15803d; margin-top:4px;">${{ "%.2f"|format(total_cobrado_hoy) }}</div>
            </div>
            <div style="background:#fef2f2; border:1px solid #fecaca; padding:16px; border-radius:8px;">
                <div style="font-size:11px; color:#b91c1c; font-weight:700;">- Gastos totales</div>
                <div style="font-size:24px; font-weight:800; color:#b91c1c; margin-top:4px;">${{ "%.2f"|format(total_gastos_hoy) }}</div>
            </div>
        </div>
        <div style="background:#f0f9ff; border:1px solid #bae6fd; padding:18px; border-radius:8px; text-align:center;">
            <div style="font-size:12px; color:#0369a1; font-weight:700;">Efectivo Neto a Entregar</div>
            <div style="font-size:28px; font-weight:900; color:#0c4a6e; margin-top:4px;">${{ "%.2f"|format(total_cobrado_hoy - total_gastos_hoy) }}</div>
        </div>
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
    <link rel="stylesheet" href="https://cloudflare.com">
    <style>
        * { box-sizing: border-box; font-family: 'Plus Jakarta Sans', sans-serif; margin: 0; padding: 0; }
        body { background-color: #f8fafc; min-height: 100vh; overflow-x: hidden; color: #1e293b; }
        .header { background: #0f172a; padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; color: white; }
        .btn-menu { background: rgba(255,255,255,0.08); color: white; border: 1px solid rgba(255,255,255,0.15); padding: 8px 16px; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; }
        .brand-title { font-size: 14px; font-weight: 800; letter-spacing: 1px; color: #38bdf8; }
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.4); backdrop-filter: blur(4px); z-index: 300; }
        .drawer { position: fixed; top: 0; left: -280px; width: 280px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s; display: flex; flex-direction: column; }
        .drawer-header { background: #072146; color: white; padding: 24px 20px; }
        .drawer-menu { list-style: none; padding: 15px 0; }
        .drawer-menu li a { display: flex; align-items: center; gap: 12px; padding: 14px 20px; color: #334155; text-decoration: none; font-weight: 700; font-size: 13.5px; }
        .main-wrapper { padding: 20px; max-width: 1200px; margin: 0 auto; width: 100%; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.4); backdrop-filter: blur(4px); z-index: 400; justify-content: center; align-items: center; padding: 16px; }
        .modal-content { background: white; padding: 24px; border-radius: 12px; width: 100%; max-width: 420px; border: 1px solid #e2e8f0; }
        .btn-primary { width: 100%; padding: 11px; color: white; border: none; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; }
        .card { background: #ffffff; padding: 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 12px; border: 1px solid #e2e8f0; }
        .cliente-card { border-left: 4px solid #0f2b5c; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .badge-mora { background: #fee2e2; color: #dc2626; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px; }
        .badge-al-dia { background: #d1fae5; color: #059669; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px; }
        .btn-filtro { background: #e2e8f0; border: none; padding: 8px 16px; border-radius: 8px; font-size: 12px; font-weight: bold; color: #475569; cursor: pointer; }
        .btn-filtro.active { background: #0f2b5c; color: white; }
        .btn-accion { padding: 8px 14px; border: none; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; color: white; }
        .btn-pagar { background: #10b981; }
        .btn-abono { background: #0ea5e9; }
        .btn-nopagar { background: #ef4444; }
        .search-box { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; margin-bottom: 16px; font-size: 13.5px; outline: none; }
        input[type="text"], input[type="number"], input[type="date"], select { width: 100%; padding: 10px 14px; margin: 6px 0 16px 0; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13.5px; color: #334155; outline: none; }
    </style>
</head>
"""
HTML_TEMPLATE += """
<body>
    <!-- BARRA SUPERIOR PREMIUM -->
    <div class="header">
        <button class="btn-menu" onclick="toggleDrawer()"><i class="fa-solid fa-bars"></i> Menú</button>
        <div class="brand-title"><b>AVANTA PAGOS</b></div>
    </div>
    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header"><h3>📍 AVANTA PAGOS</h3></div>
        <ul class="drawer-menu">
            <li><a href="/">🗺️ Ruta de Cobranza</a></li>
            <li><a href="/nuevo">👤 Nuevo Crédito</a></li>
            <li><a href="/gastos">🧮 Registrar Gastos</a></li>
            <li><a href="/cierre">💼 Cierre de Caja</a></li>
            <li><a href="/resumen">📋 Reportes</a></li>
        </ul>
    </div>
    <div class="main-wrapper">{{ contenido_html | safe }}</div>

    <div class="modal" id="modalAbono">
        <div class="modal-content">
            <h4>✏️ Registrar Abono</h4>
            <p style="font-size: 12px; color: #64748b; margin-top: 4px;">Pendiente cuota: <span id="abonoPendienteText"></span></p>
            <input type="hidden" id="abonoClienteId"><input type="hidden" id="abbonoNumCuota">
            <input type="number" step="any" id="abonoMontoInput">
            <button class="btn-primary" style="background: #10b981; margin-top:10px;" onclick="confirmarAbono()">Confirmar Abono</button>
            <button class="btn-primary" style="background: #64748b; margin-top:6px;" onclick="cerrarModal('modalAbono')">Cancelar</button>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content" style="text-align: center;">
            <h3>¡Recaudo Exitoso!</h3>
            <p id="modalMsg" style="font-size: 13px; color: #475569; margin: 10px 0;"></p>
            <a id="modalWsBtn" href="#" target="_blank" style="text-decoration: none;"><button class="btn-primary" style="background: #22c55e;"><i class="fa-brands fa-whatsapp"></i> Enviar Recibo WhatsApp</button></a>
            <button class="btn-primary" style="background: #0f172a; margin-top:6px;" onclick="window.location.reload();">Cerrar</button>
        </div>
    </div>

    <script>
        function toggleDrawer() {
            const dr = document.getElementById('drawer');
            const ov = document.getElementById('drawerOverlay');
            dr.style.left = dr.style.left === '0px' ? '-280px' : '0px';
            ov.style.display = ov.style.display === 'block' ? 'none' : 'block';
        }
        function filtrarClientes() {
            const q = document.getElementById('searchInput').value.toLowerCase();
            document.querySelectorAll('.cliente-card').forEach(c => {
                c.style.display = c.getAttribute('data-nombre').includes(q) ? 'block' : 'none';
            });
        }
        function filtrarEstado(t) {
            document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('active'));
            document.getElementById('f-' + t).classList.add('active');
            document.querySelectorAll('.cliente-card').forEach(c => {
                const m = c.getAttribute('data-mora') === '1';
                c.style.display = (t === 'todos' || (t === 'mora' && m) || (t === 'aldia' && !m)) ? 'block' : 'none';
            });
        }
        function calcularCuota() {
            const m = parseFloat(document.getElementById('calcMonto').value) || 0;
            const i = parseFloat(document.getElementById('calcInteres').value) || 0;
            const c = parseInt(document.getElementById('calcCuotas').value) || 0;
            if(m > 0 && c > 0) {
                document.getElementById('simulacionText').innerText = '$' + (m * (1 + (i / 100)) / c).toFixed(2) + ' / cuota';
            }
        }
        function ejecutarPago(id, n, v) { procesarPagoAPI(id, n, v); }
        function abrirModalAbono(id, n, p) {
            document.getElementById('abonoClienteId').value = id;
            document.getElementById('abbonoNumCuota').value = n;
            document.getElementById('abonoPendienteText').innerText = '$' + p.toFixed(2);
            document.getElementById('abonoMontoInput').value = p;
            document.getElementById('modalAbono').style.display = 'flex';
        }
        function confirmarAbono() {
            const id = document.getElementById('abonoClienteId').value;
            const n = document.getElementById('abbonoNumCuota').value;
            const m = parseFloat(document.getElementById('abonoMontoInput').value);
            if(m > 0) { cerrarModal('modalAbono'); procesarPagoAPI(id, n, m); }
        }
        function procesarPagoAPI(id, n, m) {
            fetch('/api/marcar_pago/' + id + '/' + n + '?monto=' + m).then(res => res.json()).then(d => {
                if(d.status === 'ok') {
                    document.getElementById('modalMsg').innerText = 'Pago registrado exitosamente.';
                    // CORRECCIÓN DEFINITIVA: Forzamos la limpieza absoluta del número y añadimos el separador correcto
                    const telefonoLimpio = d.recibo.telefono.toString().replace(/[^0-9]/g, '').trim();
                    document.getElementById('modalWsBtn').href = 'https://wa.me' + telefonoLimpio + '?text=' + encodeURIComponent(d.recibo.mensaje_ws);
                    document.getElementById('modalWs').style.display = 'flex';
                }
            });
        }
        function ejecutarNoPago(id) {
            fetch('/api/marcar_no_pago/' + id).then(res => res.json()).then(d => {
                if(d.status === 'ok') window.location.reload();
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
        total_cobrado_hoy = res_cobrado[0] if res_cobrado and res_cobrado[0] is not None else 0.0

        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos[0] if res_gastos and res_gastos[0] is not None else 0.0
    conn.close()

    clientes_filtrados = [
        c for c in todos_clientes
        if any(not p["pagado"] for p in c["pagos"]) and not c.get("gestionado_hoy", False)
    ]
    capital_en_calle = sum(max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"])) for c in todos_clientes)

    contexto = dict(vista="lista", clientes=clientes_filtrados, total_cobrado_hoy=total_cobrado_hoy, total_gastos_hoy=total_gastos_hoy, capital_en_calle=capital_en_calle, fecha_hoy=hoy_str, gastos_list=[])
    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)

@app.route("/nuevo")
def nuevo_cliente():
    hoy_str = date.today().isoformat()
    contexto = dict(vista="nuevo", cliente=None, fecha_hoy=hoy_str, total_cobrado_hoy=0.0, total_gastos_hoy=0.0, capital_en_calle=0.0, clientes=[], gastos_list=[])
    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)

@app.route("/gastos")
def vista_gastos():
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM gastos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
        gastos_list = cursor.fetchall()
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos[0] if res_gastos and res_gastos[0] is not None else 0.0
    conn.close()
    contexto = dict(vista="gastos", gastos_list=gastos_list, total_gastos_hoy=total_gastos_hoy, total_cobrado_hoy=0.0, capital_en_calle=0.0, clientes=[])
    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)
@app.route("/resumen")
def resumen():
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes")
        clientes = cursor.fetchall()
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM clientes")
        res_capital = cursor.fetchone()
        total_capital = res_capital[0] if res_capital and res_capital[0] is not None else 0.0
        cursor.execute("SELECT COALESCE(SUM(monto_total), 0) FROM clientes")
        res_creditos = cursor.fetchone()
        total_creditos = res_creditos[0] if res_creditos and res_creditos[0] is not None else 0.0
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos")
        res_cobrado = cursor.fetchone()
        total_cobrado = res_cobrado[0] if res_cobrado and res_cobrado[0] is not None else 0.0
    conn.close()
    contexto = dict(vista="resumen", clientes=clientes, total_capital=total_capital, total_creditos=total_creditos, total_cobrado=total_cobrado, total_cobrado_hoy=0.0, total_gastos_hoy=0.0, capital_en_calle=0.0, gastos_list=[])
    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)

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
    contexto = dict(vista='cierre', total_cobrado_hoy=total_cobrado_hoy, total_gastos_hoy=total_gastos_hoy, fecha_hoy=hoy_str, capital_en_calle=0.0, clientes=[], gastos_list=[])
    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)

@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    monto_ingresado = float(request.args.get("monto", 0))
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
        cliente = cursor.fetchone()
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s AND numero = %s", (cliente_id, num_cuota))
        pago = cursor.fetchone()
        if pago:
            nuevo_valor_pagado = pago["valor_pagado"] + monto_ingresado
            esta_pagado = 1 if nuevo_valor_pagado >= (pago["valor"] - 0.01) else 0
            cursor.execute("UPDATE pagos SET valor_pagado = %s, pagado = %s, fecha_pago_real = %s WHERE cliente_id = %s AND numero = %s", (nuevo_valor_pagado, esta_pagado, hoy_str, cliente_id, num_cuota))
            cursor.execute("UPDATE clientes SET saltado_hoy = 0, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
            conn.commit()
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s", (cliente_id,))
        pagos = cursor.fetchall()
    pagado_total = sum(p["valor_pagado"] for p in pagos)
    saldo_restante = max(0.0, cliente["monto_total"] - pagado_total)
    texto_ws = f" *AVANTA PAGOS - COMPROBANTE*\n\nCliente: *{cliente['nombre']}*\n🔹 Cuota: {num_cuota}/{len(pagos)}\n🔹 Recaudado: ${monto_ingresado:.2f}\n🔹 Saldo: ${saldo_restante:.2f}"
        # Forzamos la limpieza absoluta quitando paréntesis, comillas o comas que Neon devuelva en la tupla
    tel_sucio = str(cliente['telefono'] if 'telefono' in cliente else cliente[3])
    tel_real = tel_sucio.replace("(", "").replace(")", "").replace("'", "").replace(",", "").replace('"', '').strip()

    recibo = {
        "cliente": cliente['nombre'] if 'nombre' in cliente else cliente[2],
        "cuota": num_cuota,
        "monto": monto_ingresado,
        "saldo": saldo_restante,
        "telefono": tel_real,
        "mensaje_ws": texto_ws,
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
        latitud = request.form.get("latitud", "").strip()
        longitud = request.form.get("longitud", "").strip()

        monto_total = round(monto * (1 + (interes_porcentaje / 100)), 2)
        valor_base = round(monto_total / cuotas, 2)
        acumulado = 0.0

        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(orden), 0) FROM clientes")
            res_max = cursor.fetchone()
            max_orden = res_max[0] if res_max and res_max[0] is not None else 0
            cursor.execute(
                """
                INSERT INTO clientes (orden, nombre, telefono, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_cuota, fecha_inicio, saltado_hoy, fecha_gestion, latitud, longitud)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, '', %s, %s)
                RETURNING id
                """, 
                (max_orden + 1, nombre, telefono, identificacion, monto, interes_porcentaje, monto_total, cuotas, frecuencia, valor_base, fecha_inicio.isoformat(), latitud, longitud)
            )
            res_id = cursor.fetchone()
            cliente_id = res_id[0]

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

@app.route("/guardar_gasto", methods=["POST"])
def guardar_gasto():
    try:
        categoria = request.form.get("categoria", "Otros")
        concepto = request.form.get("concepto", "").strip()
        monto = float(request.form.get("monto", "0").replace(",", "."))
        hoy_str = date.today().isoformat()
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

@app.route("/respaldo")
def respaldo():
    return redirect("/resumen")

if __name__ == "__main__":
    app.run(debug=True, port=8080)