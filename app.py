import base64
import os
from psycopg2cffi import compat; compat.make_psycopg_green(); import psycopg2
from psycopg2.extras import DictCursor # type: ignore
import urllib.parse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, send_file

app = Flask(__name__)

# 🔑 Tu enlace real de Neon verificado para producción
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_R4oN0OiVIQcB@ep-weathered-night-b4hv7m66.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

def get_db():
    # Conexión directa a la nube de Neon con cursor de diccionarios
    return psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)

def init_db():
    # Creamos las tablas con la sintaxis PostgreSQL estándar para producción
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

CONTENIDO_HTML = """
{% if vista == 'lista' %}
    <!-- Resumen de Saldos Corporativos -->
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

    <!-- Filtros de Estado Ejecutivos -->
    <div style="display:flex; gap:8px; margin-bottom:14px; overflow-x:auto; padding-bottom:2px;">
        <button onclick="filtrarEstado('todos')" class="btn-filtro active" id="f-todos" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">👥 Por Cobrar ({{ clientes | length }})</button>
        <button onclick="filtrarEstado('mora')" class="btn-filtro" id="f-mora" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">⚠️ Mora</button>
        <button onclick="filtrarEstado('aldia')" class="btn-filtro" id="f-aldia" style="padding:8px 16px; border-radius:8px; font-size:12px; font-weight:700;">✅ Al Día</button>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente, ID o negocio..." onkeyup="filtrarClientes()" style="padding:12px; border-radius:8px; margin-bottom:16px;">

    <!-- Listado Horizontal Extendido -->
    <div id="clientesContainer" style="display:flex; flex-direction:column; gap:12px;">
        {% for c in clientes %}
            {% set pagado = c.pagos | map(attribute='valor_pagado') | sum %}
            {% set saldo = c.monto_total - pagado %}
            {% set cuota_pendiente = c.pagos | selectattr('pagado', 'equalto', 0) | list | first %}
            {% set es_mora = c.cuotas_atrasadas > 0 %}
"""
CONTENIDO_HTML += """
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
                        {% set msg_recordatorio = "Hola " ~ c.nombre ~ ", recordatorio de pago. Tu cuota pendiente es de $" ~ "%.2f"|format(cuota_pendiente.valor - cuota_pendiente.valor_pagado) ~ ". Saldo pendiente: $" ~ "%.2f"|format(saldo) ~ ". ¡Gracias!" %}
                        <a href="https://wa.me{{ c.telefono }}?text={{ msg_recordatorio | urlencode }}" target="_blank" style="text-decoration:none; font-size:12px; color:#0284c7; font-weight:700; display:flex; align-items:center; gap:4px;">
                            <i class="fa-brands fa-whatsapp" style="font-size:14px;"></i> Recordatorio
                        </a>
                    {% else %}
                        <div></div>
                    {% endif %}
                    <div style="display:flex; gap:8px;">
                        {% if cuota_pendiente %}
                            <button class="btn-accion btn-pagar" onclick="ejecutarPago({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})" style="padding:8px 14px; border-radius:6px; font-weight:700; background:#10b981; color:#ffffff; cursor:pointer;"><i class="fa-solid fa-money-bill-wave" style="margin-right:4px;"></i> Recaudar</button>
                            <button class="btn-accion btn-abono" onclick="abrirModalAbono({{ c.id }}, {{ cuota_pendiente.numero }}, {{ cuota_pendiente.valor - cuota_pendiente.valor_pagado }})" style="padding:8px 14px; border-radius:6px; font-weight:700; background:#0ea5e9; color:#ffffff; cursor:pointer;"><i class="fa-solid fa-pen-to-square" style="margin-right:4px;"></i> Abono</button>
                            <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({{ c.id }})" style="padding:8px 14px; border-radius:6px; font-weight:700; background:#ef4444; color:#ffffff; cursor:pointer;"><i class="fa-solid fa-angles-right" style="margin-right:4px;"></i> Saltar</button>
                        {% endif %}
                    </div>
                </div>
            </div>
        {% else %}
            <p style="text-align:center; color:#64748b; margin-top:24px; font-weight:500;">✅ ¡Excelente! No tienes cobranzas pendientes en este momento.</p>
        {% endfor %}
    </div>
"""
CONTENIDO_HTML += """
{% elif vista == 'nuevo' or vista == 'renovar' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">{% if vista == 'renovar' %}🔄 Renovar Crédito a {{ cliente.nombre }}{% else %}👤 Registro de Crédito / Venta{% endif %}</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
        <form action="{% if vista == 'renovar' %}/procesar_renovacion{% else %}/guardar{% endif %}" method="POST">
            {% if vista == 'renovar' %}<input type="hidden" name="cliente_id" value="{{ cliente.id }}">{% endif %}
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Nombre del Cliente</label>
            <input type="text" name="nombre" value="{{ cliente.nombre if cliente else '' }}" placeholder="Ej: Juan Perez" required>
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Teléfono WhatsApp</label>
            <input type="text" name="telefono" value="{{ cliente.telefono if cliente else '' }}" placeholder="Ej: 593991234567">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Identificación / Centro de Negocio</label>
            <input type="text" name="identificacion" value="{{ cliente.identificacion if cliente else '' }}" placeholder="Ej: Cédula o RUC">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Monto Financiado ($)</label>
            <input type="number" step="any" id="calcMonto" name="monto" placeholder="Ej: 300" oninput="calcularCuota()" required>

            <label style="font-size:12px; font-weight:700; color:#475569; margin-top:8px; display:block;">Porcentaje de Interés (%)</label>
            <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="{{ cliente.interes_porcentaje if cliente else '20' }}" oninput="calcularCuota()" required>

            <label style="font-size:12px; font-weight:700; color:#475569; margin-top:8px; display:block;">Número de Cuotas</label>
            <input type="number" id="calcCuotas" name="cuotas" value="{{ cliente.cuotas if cliente else '24' }}" oninput="calcularCuota()" required>

            <label style="font-size:12px; font-weight:700; color:#475569; margin-top:8px; display:block;">Frecuencia de Pago</label>
            <select name="frecuencia" id="calcFrecuencia" style="width:100%; padding:10px; border-radius:8px; border:1px solid #cbd5e1; margin-bottom:12px;">
                <option value="Diaria" {% if cliente and cliente.frecuencia == 'Diaria' %}selected{% endif %}>Diaria (Lunes a Sábado)</option>
                <option value="Semanal" {% if cliente and cliente.frecuencia == 'Semanal' %}selected{% endif %}>Semanal</option>
                <option value="Quincenal" {% if cliente and cliente.frecuencia == 'Quincenal' %}selected{% endif %}>Quincenal</option>
                <option value="Mensual" {% if cliente and cliente.frecuencia == 'Mensual' %}selected{% endif %}>Mensual</option>
            </select>

            <label style="font-size:12px; font-weight:700; color:#475569; display:block;">Fecha de Inicio</label>
            <input type="date" name="fecha_inicio" value="{{ fecha_hoy }}" required>

            <input type="hidden" id="input_latitud" name="latitud" value="">
            <input type="hidden" id="input_longitud" name="longitud" value="">
            
            <div style="margin:12px 0; display:flex; align-items:center; gap:8px;">
                <button type="button" onclick="capturarCoordenadasReal()" style="padding:8px 14px; background:#0ea5e9; color:white; border:none; border-radius:8px; font-size:12px; font-weight:700; cursor:pointer;"><i class="fa-solid fa-location-crosshairs"></i> Capturar GPS</button>
                <input type="text" id="gps_status_text" value="❌ Sin Ubicación" readonly style="width:auto; display:inline-block; border:none; background:transparent; font-size:12px; color:#64748b; padding:0; margin:0; font-weight:600;">
            </div>

            <div style="margin:16px 0; font-size:15px; font-weight:800; color:#10b981; background:#f0fdf4; padding:10px 14px; border-radius:8px; display:inline-block;" id="simulacionText">$0.00 / cuota</div>

            <button type="submit" class="btn-primary" style="background:#0f172a; width:100%; padding:12px; font-weight:700; border-radius:8px; border:none; color:white; cursor:pointer; font-size:14px; box-shadow:0 4px 12px rgba(15,23,42,0.15);"><i class="fa-solid fa-floppy-disk" style="margin-right:6px;"></i> Guardar Crédito</button>
        </form>
    </div>
{% elif vista == 'gastos' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">📉 Registro de Egresos / Gastos</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.02); margin-bottom:16px;">
        <form action="/guardar_gasto" method="POST" enctype="multipart/form-data">
            <label style="font-size:12px; font-weight:700; color:#475569;">Categoría del Gasto</label>
            <select name="categoria" style="width:100%; padding:10px; border-radius:8px; border:1px solid #cbd5e1; margin-bottom:12px;">
                <option value="Combustible">Combustible / Tanqueo</option>
                <option value="Viaticos">Viáticos / Comida</option>
                <option value="Papeleria">Papelería / Impresiones</option>
                <option value="Otros">Otros</option>
            </select>

            <label style="font-size:12px; font-weight:700; color:#475569;">Descripción / Detalle <span style="font-weight:normal; color:#94a3b8;">(Opcional)</span></label>
            <input type="text" name="concepto" placeholder="Ej: Tanqueo de moto (Opcional)">
            
            <label style="font-size:12px; font-weight:700; color:#475569;">Monto del Egreso ($)</label>
            <input type="number" step="any" name="monto" placeholder="Ej: 15.00" required>
            
            <label style="font-size:12px; font-weight:700; color:#475569; display:block; margin-bottom:4px;">📸 Foto de Factura / Comprobante <span style="font-weight:normal; color:#94a3b8;">(Opcional)</span></label>
            <input type="file" name="foto_comprobante" accept="image/*" capture="environment" style="padding:8px; border:1px dashed #cbd5e1; width:100%; border-radius:8px; background:#f8fafc; font-size:12px; color:#64748b;">

            <button type="submit" class="btn-primary" style="background:#ef4444; margin-top:14px; box-shadow:0 4px 12px rgba(239,68,68,0.15);"><i class="fa-solid fa-plus" style="margin-right:6px;"></i> Registrar Egreso</button>
        </form>
    </div>
"""
CONTENIDO_HTML += """
    <div style="background:#fef2f2; border:1px solid #fecaca; padding:14px 16px; border-radius:10px; display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
        <span style="font-size:13px; font-weight:700; color:#991b1b;">Total Egresos Registrados Hoy:</span>
        <span style="font-size:20px; font-weight:800; color:#dc2626;">-${{ "%.2f"|format(total_gastos_hoy) }}</span>
    </div>

    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
        <h4 style="margin-top:0; font-size:14px; color:#0f172a; font-weight:700; margin-bottom:12px;">📋 Historial de Egresos de Hoy</h4>
        {% if gastos_list %}
            <div style="overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead>
                        <tr style="background:#f8fafc; text-align:left; color:#475569; border-bottom:2px solid #e2e8f0;">
                            <th style="padding:10px 8px; font-weight:700;">Categoría</th>
                            <th style="padding:10px 8px; font-weight:700;">Detalle</th>
                            <th style="padding:10px 8px; text-align:right; font-weight:700;">Monto</th>
                            <th style="padding:10px 8px; text-align:center; font-weight:700;">Foto</th>
                            <th style="padding:10px 8px; text-align:center; font-weight:700;">Borrar</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for g in gastos_list %}
                        <tr style="border-bottom:1px solid #f1f5f9;">
                            <td style="padding:10px 8px;"><span style="background:#f1f5f9; color:#475569; padding:3px 8px; border-radius:6px; font-size:11px; font-weight:700;">{{ g.categoria }}</span></td>
                            <td style="padding:10px 8px; color:#1e293b; font-weight:600;">{{ g.concepto or 'Sin detalle' }}</td>
                            <td style="padding:10px 8px; text-align:right; color:#dc2626; font-weight:700;">-${{ "%.2f"|format(g.monto) }}</td>
                            <td style="padding:10px 8px; text-align:center;">
                                {% if g.comprobante %}
                                    <button type="button" onclick="verFoto('{{ g.comprobante }}')" style="padding:4px 8px; font-size:11px; background:#0ea5e9; color:white; border:none; border-radius:6px; font-weight:700; cursor:pointer;"><i class="fa-solid fa-image"></i> Ver</button>
                                {% else %}
                                    <span style="color:#94a3b8; font-size:11px; font-weight:500;">Sin foto</span>
                                {% endif %}
                            </td>
                            <td style="padding:10px 8px; text-align:center;">
                                <a href="/eliminar_gasto/{{ g.id }}" onclick="return confirm('¿Eliminar este gasto?')" style="text-decoration:none; color:#94a3b8; font-size:14px;"><i class="fa-solid fa-trash-can"></i></a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        {% else %}
            <p style="font-size:12px; color:#64748b; text-align:center; margin:14px 0; font-weight:500;">No hay egresos registrados el día de hoy.</p>
        {% endif %}
    </div>

{% elif vista == 'resumen' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px;">📊 Reportes y Copia de Seguridad</h3>
    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.02); margin-bottom:16px; line-height:1.8; font-size:14px; color:#334155;">
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>👥 <b>Clientes en Sistema:</b></span> <span style="font-weight:700; color:#1e293b;">{{ clientes | length }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>🏙️ <b>Capital Total Prestado:</b></span> <span style="font-weight:700; color:#1e293b;">${{ "%.2f"|format(total_capital) }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>💰 <b>Cartera Total con Interés:</b></span> <span style="font-weight:700; color:#0f2b5c;">${{ "%.2f"|format(total_creditos) }}</span></div>
        <div style="display:flex; justify-content:space-between; border-bottom:1px solid #f1f5f9; padding:10px 0;"><span>✅ <b>Total Recaudado:</b></span> <span style="color:#10b981; font-weight:700;">${{ "%.2f"|format(total_cobrado) }}</span></div>
        <div style="display:flex; justify-content:space-between; padding:10px 0;"><span>📌 <b>Pendiente de Recaudo:</b></span> <span style="color:#ef4444; font-weight:700;">${{ "%.2f"|format(total_creditos - total_cobrado) }}</span></div>
    </div>

    <div class="card" style="padding:20px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
        <h4 style="margin-top:0; font-weight:700; color:#0f172a; margin-bottom:12px;">💾 Copia de Seguridad</h4>
        <p style="font-size:12px; color:#64748b; margin-bottom:14px; font-weight:500;">Descarga un respaldo completo de la base de datos estructurada en SQLite (.db) para salvaguardar tu cartera.</p>
        <a href="/respaldo" style="text-decoration:none;"><button class="btn-primary" style="background:#0f172a; display:inline-flex; align-items:center; gap:8px; width:auto; padding:10px 18px; border-radius:8px; font-size:13px; font-weight:700; box-shadow:0 4px 12px rgba(15,23,42,0.1);"><i class="fa-solid fa-cloud-arrow-down"></i> Descargar Base de Datos (.db)</button></a>
    </div>

{% elif vista == 'cierre' %}
    <h3 style="margin-top:0; color:#0f172a; font-weight:700; margin-bottom:16px; display:flex; align-items:center; gap:8px;">
        💼 Cierre de Caja y Recaudo Diario
    </h3>
    <div class="card" style="padding:24px; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; box-shadow:0 4px 6px -1px rgba(0,0,0,0.02);">
        <div style="font-size:12px; color:#64748b; font-weight:600; margin-bottom:16px;">
            Fecha de Auditoría: <span style="color:#1e293b;">{{ fecha_hoy }}</span>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px;">
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:16px; border-radius:8px;">
                <div style="font-size:11px; color:#166534; font-weight:700;">+ Total Recaudado</div>
                <div style="font-size:24px; font-weight:800; color:#15803d; margin-top:4px;">${{ "%.2f"|format(total_cobrado_hoy) }}</div>
            </div>
            <div style="background:#fef2f2; border:1px solid #fecaca; padding:16px; border-radius:8px;">
                <div style="font-size:11px; color:#991b1b; font-weight:700;">- Gastos totales</div>
                <div style="font-size:24px; font-weight:800; color:#b91c1c; margin-top:4px;">${{ "%.2f"|format(total_gastos_hoy) }}</div>
            </div>
        </div>
        <div style="background:#f0f9ff; border:1px solid #bae6fd; padding:18px; border-radius:8px; text-align:center; margin-bottom:20px;">
            <div style="font-size:12px; color:#0369a1; font-weight:700; letter-spacing:0.5px;">Efectivo Neto a Entregar (Caja Final)</div>
            <div style="font-size:28px; font-weight:900; color:#0c4a6e; margin-top:4px;">${{ "%.2f"|format(total_cobrado_hoy - total_gastos_hoy) }}</div>
        </div>
        <button class="btn-primary" style="background:#0f2b5c; display:flex; align-items:center; justify-content:center; gap:8px; width:100%; padding:14px; border-radius:8px; font-size:14px; font-weight:700; border:none; color:white; cursor:pointer; box-shadow:0 4px 12px rgba(15,43,92,0.15);" onclick="alert('🔒 Caja cerrada con éxito. Totales consolidados y respaldados.')">
            <i class="fa-solid fa-lock"></i> Finalizar y Cerrar Caja Hoy
        </button>
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
        body { background-color: #f8fafc; min-height: 100vh; overflow-x: hidden; color: #1e293b; position: relative; }
        .header { background: #0f172a; padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; color: white; box-shadow: 0 4px 20px rgba(15, 23, 42, 0.08); }
        .btn-menu { background: rgba(255,255,255,0.08); color: white; border: 1px solid rgba(255,255,255,0.15); padding: 8px 16px; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; transition: all 0.2s; }
        .brand-title { font-size: 13px; font-weight: 800; letter-spacing: 1px; color: #ffffff; text-align: right; line-height: 1.3; }
        .badge-linea { background-color: #0ea5e9; color: #ffffff; font-size: 0.65rem; font-weight: 800; padding: 4px 10px; border-radius: 6px; letter-spacing: 0.5px; margin-left: 10px; box-shadow: 0 2px 8px rgba(14, 165, 233, 0.3); }
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.4); backdrop-filter: blur(4px); z-index: 300; }
        .drawer { position: fixed; top: 0; left: -280px; width: 280px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1); box-shadow: 10px 0 30px rgba(15,23,42,0.05); display: flex; flex-direction: column; }
        .drawer-header { background: #072146; color: white; padding: 24px 20px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .drawer-menu { list-style: none; padding: 15px 0; margin: 0; flex: 1; }
        .drawer-menu li a { display: flex; align-items: center; gap: 12px; padding: 14px 20px; color: #334155; text-decoration: none; font-weight: 700; font-size: 13.5px; border-radius: 8px; margin: 0 10px 4px 10px; border-bottom: 1px solid #f8fafc; transition: all 0.2s; }
        .main-wrapper { flex-grow: 1; display: flex; flex-direction: column; padding: 20px; max-width: 1200px; margin: 0 auto; width: 100%; }
        .content-container { width: 100%; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.4); backdrop-filter: blur(4px); z-index: 400; justify-content: center; align-items: center; padding: 16px; }
        .modal-content { background: white; padding: 24px; border-radius: 12px; width: 100%; max-width: 420px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 10px 10px -5px rgba(0,0,0,0.04); border: 1px solid #e2e8f0; }
        .btn-primary { width: 100%; padding: 11px; color: white; border: none; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; transition: background 0.2s; }
        .card { background: #ffffff; padding: 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 12px; border: 1px solid #e2e8f0; }
        .cliente-card { border-left: 4px solid #0f2b5c; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .badge-mora { background: #fee2e2; color: #dc2626; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px; }
        .badge-al-dia { background: #d1fae5; color: #059669; font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px; }
        .btn-filtro { background: #e2e8f0; border: none; padding: 8px 16px; border-radius: 8px; font-size: 12px; font-weight: bold; color: #475569; cursor: pointer; white-space: nowrap; }
        .btn-filtro.active { background: #0f2b5c; color: white; }
        .btn-accion { padding: 8px 14px; border: none; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; color: white; }
        .btn-pagar { background: #10b981; }
        .btn-abono { background: #0ea5e9; }
        .btn-nopagar { background: #ef4444; }
        .search-box { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; margin-bottom: 16px; font-size: 13.5px; outline: none; background: white; }
        input[type="text"], input[type="number"], input[type="date"], select { width: 100%; padding: 10px 14px; margin: 6px 0 16px 0; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13.5px; color: #334155; outline: none; }
    </style>
</head>
"""
HTML_TEMPLATE += """
<body>
    <!-- BARRA SUPERIOR PREMIUM -->
    <div class="header">
        <button class="btn-menu" onclick="toggleDrawer()">
            <i class="fa-solid fa-bars" style="margin-right: 6px;"></i> Menú
        </button>
        <div style="display: flex; align-items: center;">
            <div class="brand-title">
                <span style="font-size: 10px; font-weight: 500; color: #94a3b8; display: block; letter-spacing: 2px;">AVANTA</span>
                <span style="font-weight: 800; font-size: 15px; color: #38bdf8; letter-spacing: 0.5px;">PAGOS</span>
            </div>
            <span class="badge-linea">✨ En Línea</span>
        </div>
    </div>

    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>

    <!-- PANEL LATERAL DESPLEGABLE -->
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <h3 style="margin: 0; font-size: 16px; font-weight: 800; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px;">📍 AVANTA PAGOS</h3>
            <p style="margin: 4px 0 0 0; font-size: 0.75rem; color: #cbd5e1; font-weight: 600;">Centro de Negocio (CN-01)</p>
        </div>
        <ul class="drawer-menu">
            <li><a href="/" onclick="toggleDrawer()"><span style="width: 24px; display: inline-block;">🗺️</span> Ruta de Cobranza</a></li>
            <li><a href="/nuevo" onclick="toggleDrawer()"><span style="width: 24px; display: inline-block;">👤</span> Nuevo Crédito / Venta</a></li>
            <li><a href="/gastos" onclick="toggleDrawer()"><span style="width: 24px; display: inline-block;">🧮</span> Registrar Gastos</a></li>
            <li><a href="/cierre" onclick="toggleDrawer()"><span style="width: 24px; display: inline-block;">💼</span> Cierre y Recaudo Diario</a></li>
            <li><a href="/resumen" onclick="toggleDrawer()"><span style="width: 24px; display: inline-block;">📋</span> Reporte y Backup</a></li>
        </ul>
    </div>

    <div class="main-wrapper">
        <div class="content-container">
            {{ contenido_html | safe }}
        </div>
    </div>

    <!-- VENTANAS MODALES PREMIUM -->
    <div class="modal" id="modalAbono">
        <div class="modal-content">
            <h4 style="margin-top: 0; color: #0f172a; font-weight: 700;">✏️ Registrar Abono</h4>
            <p style="font-size: 12px; color: #64748b; margin-top: 4px;">Pendiente total cuota: <span id="abonoPendienteText" style="font-weight: 700; color: #ef4444;"></span></p>
            <input type="hidden" id="abonoClienteId">
            <input type="hidden" id="abbonoNumCuota">
            <label style="font-size: 11px; font-weight: 700; color: #475569; margin-top: 12px; display: block;">Monto a Abonar ($)</label>
            <input type="number" step="any" id="abonoMontoInput">
            <div style="display: flex; gap: 8px; margin-top: 4px;">
                <button type="button" class="btn-primary" style="background: #10b981;" onclick="confirmarAbono()">Confirmar Recaudo</button>
                <button type="button" class="btn-primary" style="background: #64748b;" onclick="cerrarModal('modalAbono')">Cancelar</button>
            </div>
        </div>
    </div>

    <div class="modal" id="modalWs">
        <div class="modal-content" style="text-align: center;">
            <div style="width: 48px; height: 48px; background: #dcfce7; color: #10b981; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 12px auto; font-size: 20px;">
                <i class="fa-solid fa-circle-check"></i>
            </div>
            <h3 style="color: #0f172a; font-weight: 700; font-size: 18px;">¡Recaudo Exitoso!</h3>
            <p id="modalMsg" style="font-size: 13px; color: #475569; font-weight: 500; margin-top: 4px;"></p>
            <div style="background: #f8fafc; padding: 14px; border-radius: 8px; text-align: left; font-size: 12px; margin: 16px 0; border: 1px solid #e2e8f0; line-height: 1.6;">
                <span style="color: #64748b;">Fecha:</span> <span id="tFecha" style="float: right; font-weight: 600; color: #1e293b;"></span><br>
                <span style="color: #64748b;">Cliente:</span> <span id="tCliente" style="float: right; font-weight: 600; color: #1e293b;"></span><br>
                <span style="color: #64748b;">Cuota N°:</span> <span id="tCuota" style="float: right; font-weight: 600; color: #1e293b;"></span><br>
                <span style="color: #64748b;">Monto Recibido:</span> <span id="tMonto" style="float: right; font-weight: 700; color: #10b981;"></span><br>
                <span style="color: #64748b;">Saldo Pendiente:</span> <span id="tSaldo" style="float: right; font-weight: 700; color: #ef4444;"></span>
            </div>
            <a id="modalWsBtn" href="#" target="_blank" style="text-decoration: none;"><button class="btn-primary" style="background: #22c55e; margin-bottom: 8px;"><i class="fa-brands fa-whatsapp" style="margin-right: 6px;"></i> Enviar Recibo WhatsApp</button></a>
            <button class="btn-primary" style="background: #0f172a; margin-bottom: 8px;" onclick="imprimirTicket()"><i class="fa-solid fa-print" style="margin-right: 6px;"></i> Imprimir Ticket</button>
            <button class="btn-primary" style="background: #94a3b8;" onclick="cerrarModal('modalWs'); window.location.reload();">Cerrar Ventana</button>
        </div>
    </div>

    <div class="modal" id="modalFoto">
        <div class="modal-content" style="max-width: 460px; text-align: center;">
            <h4 style="margin-top: 0; font-weight: 700; color: #0f172a; margin-bottom: 12px;">Comprobante de Gasto</h4>
            <img id="imgComprobante" src="" style="width: 100%; max-height: 380px; object-fit: contain; border-radius: 8px; margin-bottom: 12px; border: 1px solid #e2e8f0;">
            <button type="button" class="btn-primary" style="background: #64748b;" onclick="cerrarModal('modalFoto')">Cerrar</button>
        </div>
    </div>
"""
CONTENIDO_HTML += """
    <script>
        function toggleDrawer() {
            const dr = document.getElementById('drawer');
            const ov = document.getElementById('drawerOverlay');
            if(dr.style.left === '0px') {
                dr.style.left = '-280px';
                ov.style.display = 'none';
            } else {
                dr.style.left = '0px';
                ov.style.display = 'block';
            }
        }

        function filtrarClientes() { 
            const q = document.getElementById('searchInput').value.toLowerCase(); 
            document.querySelectorAll('.cliente-card').forEach(c => { 
                c.style.display = c.getAttribute('data-nombre').includes(q) ? 'block' : 'none'; 
            }); 
        }
        
        function filtrarEstado(t) { 
            document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('active')); 
            const btn = document.getElementById('f-' + t);
            if(btn) btn.classList.add('active'); 
            document.querySelectorAll('.cliente-card').forEach(c => { 
                const m = c.getAttribute('data-mora') === '1'; 
                c.style.display = (t === 'todos' || (t === 'mora' && m) || (t === 'aldia' && !m)) ? 'block' : 'none'; 
            }); 
        }   
        
        function capturarCoordenadasReal() {
            const estatus = document.getElementById('gps_status_text');
            const inputLat = document.getElementById('input_latitud');
            const inputLon = document.getElementById('input_longitud');
            if (!navigator.geolocation) { alert("Tu dispositivo no soporta Geolocalización GPS."); return; }
            estatus.value = "⏳ Localizando...";
            navigator.geolocation.getCurrentPosition(
                function(p) {
                    inputLat.value = p.coords.latitude;
                    inputLon.value = p.coords.longitude;
                    estatus.value = '✅ Listo (' + p.coords.latitude.toFixed(4) + ', ' + p.coords.longitude.toFixed(4) + ')';
                    alert("📍 Ubicación GPS real capturada.");
                },
                function(e) { estatus.value = "❌ Error de GPS"; alert("Activa el GPS y da permisos."); },
                { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
            );
        }
        
        function calcularCuota() { 
            const m = parseFloat(document.getElementById('calcMonto').value) || 0; 
            const i = parseFloat(document.getElementById('calcInteres').value) || 0; 
            const c = parseInt(document.getElementById('calcCuotas').value) || 0; 
            if(m > 0 && c > 0) { 
                const t = m * (1 + (i / 100)); 
                document.getElementById('simulacionText').innerText = '$' + (t / c).toFixed(2) + ' / cuota'; 
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
            if(isNaN(m) || m <= 0) return; 
            cerrarModal('modalAbono'); 
            procesarPagoAPI(id, n, m); 
        }
        
        function procesarPagoAPI(id, n, m) {
            fetch('/api/marcar_pago/' + id + '/' + n + '?monto=' + m).then(res => res.json()).then(d => {
                if(d.status === 'ok') {
                    const card = document.getElementById('cliente-card-' + id);
                    if(card) card.style.display = 'none';
                    document.getElementById('modalMsg').innerText = 'Recaudo exitoso.';
                    
                    const mensajeCodificado = encodeURIComponent(d.recibo.mensaje_ws);
                    document.getElementById('modalWsBtn').href = 'https://wa.me' + d.recibo.telefono + '?text=' + mensajeCodificado;
                    
                    document.getElementById('tFecha').innerText = new Date().toLocaleDateString();
                    document.getElementById('tCliente').innerText = d.recibo.cliente;
                    document.getElementById('tCuota').innerText = d.recibo.cuota;
                    document.getElementById('tMonto').innerText = d.recibo.monto.toFixed(2);
                    document.getElementById('tSaldo').innerText = d.recibo.saldo.toFixed(2);
                    document.getElementById('modalWs').style.display = 'flex';
                }
            });
        }
        
        function ejecutarNoPago(id) { 
            fetch('/api/marcar_no_pago/' + id).then(res => res.json()).then(d => { 
                if(d.status === 'ok') document.getElementById('cliente-card-' + id).style.display = 'none'; 
            }); 
        }
        function verFoto(s) { document.getElementById('imgComprobante').src = s; document.getElementById('modalFoto').style.display = 'flex'; }
        function imprimirTicket() { window.print(); }
        function cerrarModal(id) { document.getElementById(id).style.display = 'none'; }
    </script>
</body>
</html>
"""
# ==========================================
# RUTAS DE CONTROL DEL SERVIDOR (BACKEND)
# ==========================================

@app.route("/")
def lista():
    todos_clientes = obtener_clientes_completos()
    hoy_str = date.today().isoformat()

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s",
            (hoy_str,),
        )
        res_cobrado = cursor.fetchone()
        total_cobrado_hoy = res_cobrado[0] if res_cobrado and res_cobrado[0] is not None else 0.0

        cursor.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM gastos WHERE fecha = %s",
            (hoy_str,),
        )
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos[0] if res_gastos and res_gastos[0] is not None else 0.0
    conn.close()

    clientes_filtrados = [
        c for c in todos_clientes
        if any(not p["pagado"] for p in c["pagos"]) and not c.get("gestionado_hoy", False)
    ]

    capital_en_calle = sum(
        max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"]))
        for c in todos_clientes
    )

    contexto = dict(
        vista="lista",
        clientes=clientes_filtrados,
        total_cobrado_hoy=total_cobrado_hoy,
        total_gastos_hoy=total_gastos_hoy,
        capital_en_calle=capital_en_calle,
        fecha_hoy=hoy_str,
        gastos_list=[]
    )

    contenido_renderizado = render_template_string(CONTENIDO_HTML, **contexto)
    return render_template_string(HTML_TEMPLATE, contenido_html=contenido_renderizado, **contexto)


@app.route("/nuevo")
def nuevo_cliente():
    hoy_str = date.today().isoformat()
    contexto = dict(
        vista="nuevo", 
        cliente=None, 
        fecha_hoy=hoy_str,
        total_cobrado_hoy=0.0,
        total_gastos_hoy=0.0,
        capital_en_calle=0.0,
        clientes=[],
        gastos_list=[]
    )
    
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
    
    contexto = dict(
        vista="gastos", 
        gastos_list=gastos_list, 
        total_gastos_hoy=total_gastos_hoy,
        total_cobrado_hoy=0.0,
        capital_en_calle=0.0,
        clientes=[]
    )
    
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
    
    contexto = dict(
        vista="resumen",
        clientes=clientes,
        total_capital=total_capital,
        total_creditos=total_creditos,
        total_cobrado=total_cobrado,
        total_cobrado_hoy=0.0,
        total_gastos_hoy=0.0,
        capital_en_calle=0.0,
        gastos_list=[]
    )
    
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
    
    contexto = dict(
        vista='cierre',
        total_cobrado_hoy=total_cobrado_hoy,
        total_gastos_hoy=total_gastos_hoy,
        fecha_hoy=hoy_str,
        capital_en_calle=0.0,
        clientes=[],
        gastos_list=[]
    )
    
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
        if not cliente:
            conn.close()
            return jsonify({"status": "error", "message": "Cliente no encontrado"}), 404

        cursor.execute(
            "SELECT * FROM pagos WHERE cliente_id = %s AND numero = %s",
            (cliente_id, num_cuota),
        )
        pago = cursor.fetchone()

        if pago:
            nuevo_valor_pagado = pago["valor_pagado"] + monto_ingresado
            esta_pagado = 1 if nuevo_valor_pagado >= (pago["valor"] - 0.01) else 0

            cursor.execute(
                "UPDATE pagos SET valor_pagado = %s, pagado = %s, fecha_pago_real = %s WHERE cliente_id = %s AND numero = %s",
                (nuevo_valor_pagado, esta_pagado, hoy_str, cliente_id, num_cuota),
            )
            cursor.execute(
                "UPDATE clientes SET saltado_hoy = 0, fecha_gestion = %s WHERE id = %s",
                (hoy_str, cliente_id),
            )
            conn.commit()

        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s", (cliente_id,))
        pagos = cursor.fetchall()
        
    pagado_total = sum(p["valor_pagado"] for p in pagos)
    saldo_restante = max(0.0, cliente["monto_total"] - pagado_total)

    texto_ws = (
        f" *AVANTA PAGOS - COMPROBANTE DE RECAUDO*\n\n"
        f"Cliente: *{cliente['nombre']}*\n"
        f"🔹 Cuota: {num_cuota}/{len(pagos)}\n"
        f"🔹 Recaudado: ${monto_ingresado:.2f}\n"
        f"🔹 Saldo Pendiente: ${saldo_restante:.2f}\n\n"
        f"¡Gracias por su pago!"
    )

    recibo = {
        "cliente": cliente["nombre"],
        "cuota": num_cuota,
        "monto": monto_ingresado,
        "saldo": saldo_restante,
        "telefono": cliente["telefono"],
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
    return redirect(request.referrer or "/")

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
                f_cuota = calcular_fecha(fecha_inicio, num - 1, frecuencia) # type: ignore
                cursor.execute(
                    "INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real) VALUES (%s, %s, %s, %s, 0, 0, '')", 
                    (cliente_id, num, f_cuota.isoformat(), valor_cuota)
                )
        conn.commit()
        conn.close()
        return redirect("/")
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
        
        foto_b64 = ""
        if "foto_comprobante" in request.files:
            file = request.files["foto_comprobante"]
            if file and file.filename != "":
                foto_b64 = "data:" + file.content_type + ";base64," + base64.b64encode(file.read()).decode("utf-8")
        
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO gastos (categoria, concepto, monto, fecha, comprobante) VALUES (%s, %s, %s, %s, %s)",
                (categoria, concepto, monto, hoy_str, foto_b64)
            )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return redirect("/gastos")


@app.route("/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM gastos WHERE id = %s", (gasto_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return redirect("/gastos")


@app.route("/respaldo")
def respaldo():
    return redirect("/resumen")


if __name__ == "__main__":
    app.run(debug=True, port=8080)
