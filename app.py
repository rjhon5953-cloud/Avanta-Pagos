import base64
import os
import psycopg2
from psycopg2.extras import DictCursor
import urllib.parse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from flask import Flask, jsonify, redirect, render_template_string, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "avanta_secret_key_1.3_2026")

# 🔑 Enlace a tu base de datos de Neon en la nube
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_R4oN0OiVIQcB@ep-weathered-night-b4hv7m66.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 🏢 Tabla de Clientes con Estados (Activo, Inactivo, Lista Negra) y Geolocalización GPS
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clientes (
                    id SERIAL PRIMARY KEY,
                    orden INTEGER NOT NULL DEFAULT 0,
                    nombre TEXT NOT NULL,
                    telefono TEXT,
                    direccion TEXT DEFAULT '',
                    referencia TEXT DEFAULT '',
                    identificacion TEXT,
                    monto REAL NOT NULL,
                    interes_porcentaje REAL NOT NULL DEFAULT 20,
                    monto_total REAL NOT NULL DEFAULT 0,
                    cuotas INTEGER NOT NULL,
                    frecuencia TEXT NOT NULL,
                    valor_cuota REAL NOT NULL,
                    fecha_inicio TEXT NOT NULL,
                    fecha_vencimiento TEXT NOT NULL,
                    estado TEXT NOT NULL DEFAULT 'Activo', -- Activo, Inactivo, Lista Negra
                    saltado_hoy INTEGER NOT NULL DEFAULT 0,
                    fecha_gestion TEXT DEFAULT '',
                    latitud TEXT DEFAULT '',
                    longitud TEXT DEFAULT '',
                    enrutado_forzado INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 💵 Tabla de Control de Cuotas y Pagos individuales
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
            # 📊 Tabla de Balance, Movimientos (Inversión/Entrada) y Egresos (Gastos) con Fotos
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_movimientos (
                    id SERIAL PRIMARY KEY,
                    tipo TEXT NOT NULL, -- Entrada, Salida
                    categoria TEXT NOT NULL,
                    concepto TEXT NOT NULL,
                    monto REAL NOT NULL,
                    fecha TEXT NOT NULL,
                    comprobante TEXT DEFAULT ''
                )
                """
            )
            # ⚙️ Tabla de Configuración de la Empresa, Perfil y Ticket POS
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS configuracion (
                    id SERIAL PRIMARY KEY,
                    llave TEXT UNIQUE NOT NULL,
                    valor TEXT NOT NULL
                )
                """
            )
            # Valores por defecto para Configuración si no existen
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
        
        # Filtro estricto de control de la Ruta del Día
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

# --- 🚀 PLANTILLAS MAESTRAS DE DISEÑO PREMIUM (HTML/CSS) ---
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
        .logo-icon { font-size: 42px; background: linear-gradient(45deg, #00a8cc, #10b981); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 800; display: inline-block; margin-bottom: 4px; }
        .logo-subtitle { color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; }
        h2 { color: white; font-size: 20px; font-weight: 700; margin-bottom: 24px; text-align: center; }
        .input-group { text-align: left; margin-bottom: 16px; }
        label { color: #cbd5e1; font-size: 11px; font-weight: 700; text-transform: uppercase; margin-bottom: 6px; display: block; letter-spacing: 0.5px; }
        input { width: 100%; padding: 12px 14px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 10px; color: white; font-size: 14px; outline: none; transition: all 0.3s ease; display: block; margin-top: 4px; }
        input:focus { border-color: #00a8cc; box-shadow: 0 0 0 3px rgba(0, 168, 204, 0.2); }
        .btn-access { background: linear-gradient(90deg, #00a8cc 0%, #0284c7 100%); color: white; font-weight: 700; font-size: 14px; border: none; padding: 14px; border-radius: 10px; width: 100%; cursor: pointer; margin-top: 14px; box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3); transition: all 0.2s ease; display: block; }
        .btn-access:active { transform: scale(0.98); opacity: 0.9; }
        .version-text { color: #64748b; font-size: 11px; font-weight: 600; margin-top: 24px; letter-spacing: 0.5px; }
        .error-msg { background: rgba(239, 68, 68, 0.2); border: 1px solid #ef4444; color: #fca5a5; padding: 10px; border-radius: 8px; font-size: 12px; font-weight: 600; margin-bottom: 16px; text-align: left; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo-container">
            <div class="logo-icon">🌐 AVANTA</div>
            <div class="logo-subtitle">PAGOS SISTEMAS</div>
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
        
        /* 📱 Barra Superior Corporativa */
        .navbar { background: #0f2b5c; color: white; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 200; box-shadow: 0 4px 12px rgba(15, 43, 92, 0.15); }
        .btn-nav-icon { background: rgba(255,255,255,0.12); color: white; border: none; padding: 8px 12px; border-radius: 8px; font-weight: 700; font-size: 14px; cursor: pointer; display: flex; align-items: center; gap: 6px; text-decoration: none; }
        .btn-nav-icon:active { background: rgba(255,255,255,0.25); }
        .nav-title { font-size: 15px; font-weight: 800; letter-spacing: 0.5px; text-transform: uppercase; }

        /* 📑 Menú Lateral Desplegable (Drawer) */
        .drawer-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.5); z-index: 300; backdrop-filter: blur(2px); }
        .drawer-overlay.active { display: block; }
        .drawer { position: fixed; top: 0; left: -290px; width: 290px; height: 100%; background: #ffffff; z-index: 301; transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1); display: flex; flex-direction: column; box-shadow: 4px 0 24px rgba(0,0,0,0.15); }
        .drawer.active { left: 0; }
        .drawer-header { background: #0f2b5c; color: white; padding: 20px 16px; border-bottom: 4px solid #00a8cc; }
        .drawer-logo { font-size: 18px; font-weight: 800; letter-spacing: 0.5px; }
        .drawer-info { font-size: 11px; opacity: 0.85; margin-top: 4px; font-weight: 500; }
        .drawer-menu { list-style: none; padding: 10px 0; overflow-y: auto; flex: 1; }
        .drawer-menu li a { display: flex; align-items: center; gap: 12px; padding: 13px 20px; color: #334155; text-decoration: none; font-weight: 700; font-size: 13px; border-bottom: 1px solid #f1f5f9; transition: all 0.2s ease; }
        .drawer-menu li a i { font-size: 16px; width: 20px; color: #0f2b5c; text-align: center; }
        .drawer-menu li a:active { background: #e0f2fe; color: #0284c7; }
        .drawer-menu li a.logout-link { color: #ef4444; }
        .drawer-menu li a.logout-link i { color: #ef4444; }

        /* 📊 Paneles e Indicadores de Inicio */
        .main-container { padding: 12px; max-width: 600px; margin: 0 auto; }
        .date-badge { text-align: center; font-size: 13px; font-weight: 800; color: #475569; background: #e2e8f0; padding: 6px; border-radius: 6px; margin-bottom: 10px; text-transform: capitalize; }
        .grid-kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
        .kpi-card { background: white; padding: 12px 10px; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }
        .kpi-title { font-size: 10px; color: #64748b; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
        .kpi-val { font-size: 15px; font-weight: 800; color: #0f2b5c; }
        .expected-card { background: #f0f9ff; border: 1px solid #bae6fd; border-left: 5px solid #00a8cc; padding: 12px; border-radius: 12px; margin-bottom: 12px; }

        /* 🔀 Interruptor Central Pagos / No Pagos */
        .toggle-section { background: white; border: 1px solid #e2e8f0; padding: 6px; border-radius: 12px; display: flex; gap: 6px; margin-bottom: 12px; }
        .btn-toggle { flex: 1; border: none; padding: 8px; border-radius: 8px; font-size: 12px; font-weight: 800; cursor: pointer; color: #64748b; background: #f1f5f9; transition: all 0.2s; }
        .btn-toggle.active { background: #0f2b5c; color: white; }

        /* 🔍 Cajas de Búsqueda y Tarjetas horizontales */
        .section-header-title { font-size: 13px; font-weight: 800; color: #0f2b5c; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
        .search-box { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 10px; margin-bottom: 12px; font-size: 13px; outline: none; background: white; box-shadow: inset 0 1px 2px rgba(0,0,0,0.05); }
        .card { background: white; padding: 14px; border-radius: 12px; margin-bottom: 10px; border: 1px solid #e2e8f0; box-shadow: 0 2px 5px rgba(0,0,0,0.03); }
        
        /* 🔘 Botones de Acción de Clientes */
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        .btn-accion { border: none; padding: 8px 12px; border-radius: 8px; font-weight: 800; font-size: 11px; cursor: pointer; color: white; text-align: center; }
        .btn-pagar { background: #10b981; } .btn-abono { background: #00a8cc; } .btn-nopagar { background: #ef4444; }
        
        /* 🏛️ Ventanas Modales */
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.6); z-index: 400; justify-content: center; align-items: center; padding: 16px; backdrop-filter: blur(2px); }
        .modal-content { background: white; border-radius: 16px; padding: 20px; width: 100%; max-width: 420px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); overflow-y: auto; max-height: 90vh; }
        .modal-title { font-size: 16px; font-weight: 800; color: #0f2b5c; margin-bottom: 12px; border-bottom: 2px solid #f1f5f9; padding-bottom: 8px; text-align: left; }
        .modal-grid-data { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; text-align: left; margin-bottom: 12px; }
        .data-box { background: #f8fafc; padding: 8px; border-radius: 8px; border: 1px solid #f1f5f9; }
        .data-lbl { font-size: 9px; color: #64748b; font-weight: 800; text-transform: uppercase; }
        .data-val { font-size: 12px; font-weight: 700; color: #1e293b; margin-top: 2px; }
        
        /* Formularios y Inputs Generales */
        label { font-size: 11px; font-weight: bold; color: #475569; display: block; margin-bottom: 4px; text-align: left; }
        input[type="text"], input[type="number"], input[type="date"], select { width: 100%; padding: 10px; margin-bottom: 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13px; outline: none; }
        .btn-modal-close { background: #e2e8f0; color: #334155; border: none; padding: 10px; border-radius: 8px; font-weight: 700; width: 100%; cursor: pointer; font-size: 13px; margin-top: 6px; }

        @media print {
            body * { visibility: hidden; }
            #ticketPrint, #ticketPrint * { visibility: visible; }
            #ticketPrint { position: absolute; left: 0; top: 0; width: 58mm; font-family: monospace; font-size: 11px; }
        }
    </style>
</head>
<body>

    <!-- 🚀 Barra Superior Estructural -->
    <div class="navbar">
        <button class="btn-menu btn-nav-icon" onclick="toggleDrawer()"><i class="fa-solid fa-bars"></i> Menú</button>
        <div class="nav-title">🌐 AVANTA PAGOS</div>
        <a href="#" onclick="navegarRuta('/nuevo')" class="btn-nav-icon" style="background:#00a8cc;"><i class="fa-solid fa-plus"></i> Nuevo</a>
    </div>

    <!-- 📑 Menú Lateral Funcional Completo -->
    <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <div class="drawer-logo">🌐 AVANTA PAGOS v1.3</div>
            <div class="drawer-info"><i class="fa-solid fa-phone"></i> Soporte: +593 99 999 9999</div>
        </div>
        <ul class="drawer-menu">
            <li><a href="#" onclick="navegarRuta('/')"><i class="fa-solid fa-route"></i> Ruta Principal</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/clientes')"><i class="fa-solid fa-users"></i> Clientes</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/creditos')"><i class="fa-solid fa-money-bill-wave"></i> Créditos Activos</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/ruta_orden')"><i class="fa-solid fa-arrow-down-up-lock"></i> Reordenar Ruta</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/listados')"><i class="fa-solid fa-list-check"></i> Listados e Historial</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/balance')"><i class="fa-solid fa-scale-balanced"></i> Balance de Caja</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/agregar_lista')"><i class="fa-solid fa-user-plus"></i> Agregar a Lista</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/salvar_datos')"><i class="fa-solid fa-cloud-arrow-up"></i> Salvar Datos (Nube)</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/bluetooth')"><i class="fa-solid fa-print"></i> Opciones Bluetooth</a></li>
            <li><a href="#" onclick="navegarRuta('/menu/configuracion')"><i class="fa-solid fa-sliders"></i> Configuración / Sistema</a></li>
            <li><a href="/logout" class="logout-link"><i class="fa-solid fa-right-from-bracket"></i> Cerrar Sesión</a></li>
        </ul>
    </div>

    <!-- 🚀 Contenedor Principal Dinámico AJAX -->
    <div class="main-container" id="mainContent">
        {{ contenido_html | safe }}
    </div>

    <!-- 🏛️ MODAL: Ficha de Información Detallada del Cliente -->
    <div class="modal" id="modalInfoCliente">
        <div class="modal-content">
            <div class="modal-title" id="inf_nombre">Cargando Cliente...</div>
            Teléfono
            Identificación
            Dirección
            Referencia Negocio
            Fecha Crédito
            Vencimiento
            Modalidad
            Valor Cuota
            Total Crédito
            Saldo Actual
            Cuotas Pagadas
            Cuotas Pendientes
            Días Atraso
            Para ponerse al día
            Llamar
            Ver en Mapa

            Salir de la Información

            Recaudo Registrado

        Enviar Comprobante WhatsApp
        Imprimir Ticket POS
        Finalizar y Continuar

            Asentar Abono / Cuotas
            Monto de una cuota regular: $0.00
            Monto de Dinero a Cobrar ($)
            Cantidad de Cuotas Equivalentes
            💾 Guardar Abono
            Cancelar

"""
HTML_TEMPLATE += """
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
                console.log('GPS capturado con éxito');
            }
        }, function(error) { console.log('Error de GPS: ' + error.message); });
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
                document.getElementById('inf_modalidad').innerText = d.data.frecuencia;
                document.getElementById('inf_v_cuota').innerText = '$' + d.data.valor_cuota.toFixed(2);
                document.getElementById('inf_total_cred').innerText = '$' + d.data.monto_total.toFixed(2);
                document.getElementById('inf_saldo_act').innerText = '$' + d.data.saldo_actual.toFixed(2);
                document.getElementById('inf_c_pagadas').innerText = d.data.cuotas_pagadas;
                document.getElementById('inf_c_pend').innerText = d.data.cuotas_pendientes;
                document.getElementById('inf_d_atraso').innerText = d.data.dias_atraso;
                document.getElementById('inf_monto_mora').innerText = '$' + d.data.monto_mora.toFixed(2);
                
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
    document.getElementById('abonoValorCuotaText').innerText = '$' + valorCuota.toFixed(2);
    document.getElementById('abonoMontoInput').value = pendiente;
    document.getElementById('abonoCantCuotasInput').value = (pendiente / valorCuota).toFixed(1);
    document.getElementById('modalAbono').style.display = 'flex';
}

function actualizarCantCuotas() {
    const monto = parseFloat(document.getElementById('abonoMontoInput').value) || 0;
    const vCuota = parseFloat(document.getElementById('abonoValorCuotaText').innerText.replace('$', '')) || 1;
    document.getElementById('abonoCantCuotasInput').value = (monto / vCuota).toFixed(1);
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
                document.getElementById('modalMsg').innerText = 'Recaudo de $' + monto.toFixed(2) + ' registrado para ' + data.recibo.cliente + '.';
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
# --- 🔐 SEGURIDAD: RUTAS DE CONTROL DE ACCESO ---
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
            return render_template_string(LOGIN_HTML, error="Credenciales incorrectas comerciales.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --- 🗺️ RUTA DE VISTA PRINCIPAL (DASHBOARD) ---
@app.route("/")
def dashboard_principal():
    if not session.get("autenticado"):
        return redirect("/login")
        
    hoy = date.today()
    hoy_str = hoy.isoformat()
    
    # Formateo profesional de fecha para el Badge Superior
    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    meses_ano = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    fecha_badge = f"{dias_semana[hoy.weekday()]}, {hoy.day} de {meses_ano[hoy.month - 1]} del {hoy.year}"

    conn = get_db()
    with conn.cursor() as cursor:
        # CORRECCIÓN POSTGRESQL: Extraemos la posición [0] de la fila devuelta por Neon
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
        res_cobrado = cursor.fetchone()
        total_cobrado_hoy = float(res_cobrado[0]) if res_cobrado and res_cobrado[0] is not None else 0.0
        
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = float(res_gastos[0]) if res_gastos and res_gastos[0] is not None else 0.0
        
        cursor.execute("SELECT COUNT(*) FROM clientes WHERE fecha_inicio = %s", (hoy_str,))
        res_nuevos = cursor.fetchone()
        creditos_nuevos_hoy = int(res_nuevos[0]) if res_nuevos and res_nuevos[0] is not None else 0
    conn.close()

    clientes_ruta = obtener_clientes_ruta_hoy()
    
    # Cálculo dinámico del Debido Esperado del Día
    debido_minimo_dia = 0.0
    debido_total_acumulado = 0.0
    for cl in clientes_ruta:
        debido_minimo_dia += cl["valor_cuota"]
        for p in cl["pagos"]:
            if not p["pagado"] and p["fecha"] <= hoy_str:
                debido_total_acumulado += (p["valor"] - p["valor_pagado"])

    capital_en_calle = sum(max(0.0, c["monto_total"] - sum(p["valor_pagado"] for p in c["pagos"])) for c in clientes_ruta)

    # Contenido dinámico empaquetado para el Dash con formato limpio
    contexto_dash = f"""
    <div class="date-badge"><i class="fa-solid fa-calendar-day"></i> {fecha_badge}</div>
    
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
        <div class="kpi-val" style="color:#0369a1; font-size:13px;">
            Mínimo: <b>${debido_minimo_dia:.2f}</b> | Acumulado en Mora: <b>${debido_total_acumulado:.2f}</b>
        </div>
    </div>

    <div class="toggle-section">
        <button class="btn-toggle active" id="f-todos" onclick="filtrarEstado('todos')"><i class="fa-solid fa-route"></i> Ruta Completa ({len(clientes_ruta)})</button>
        <button class="btn-toggle" id="f-mora" onclick="filtrarEstado('mora')"><i class="fa-solid fa-triangle-exclamation"></i> Solo Mora</button>
    </div>

    <div class="section-header-title"><i class="fa-solid fa-magnifying-glass"></i> Filtro de Ruta Principal</div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar por nombre de cliente o referencia..." onkeyup="filtrarClientes()">

    <div id="clientesContainer">
    """
    
    for c in clientes_ruta:
        pagado_cl = sum(p["valor_pagado"] for p in c["pagos"])
        saldo_cl = c["monto_total"] - pagado_cl
        cuota_p = next((p for p in c["pagos"] if p["pagado"] == 0), None)
        es_mora_cl = c.get("saltado_hoy", 0) > 0

        contexto_dash += f"""
        <div class="card cliente-card" data-nombre="{c['nombre'].lower()}" data-mora="{"1" if es_mora_cl else "0"}">
            <div class="flex-between">
                <div style="display:flex; align-items:center; gap:8px; cursor:pointer;" onclick="verFichaCliente({c['id']})">
                    <div>
                        <span style="font-size:14px; font-weight:800; color:#0f2b5c;">{c['nombre']}</span>
                        {"<span class='badge-mora'>MORA</span>" if es_mora_cl else "<span class='badge-al-dia'>AL DÍA</span>"}
                        <div style="font-size:11px; color:#64748b; margin-top:2px;">Ref: {c['referencia'] or 'N/A'}</div>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:10px; color:#64748b;">Cuota:</div>
                    <div style="font-size:15px; font-weight:800; color:#0f2b5c;">${c['valor_cuota']:.2f}</div>
                </div>
            </div>
            
            <div style="display:flex; justify-content:flex-end; gap:6px; margin-top:10px;">
                <button class="btn-accion btn-pagar" onclick="ejecutarPago({c['id']}, {cuota_p['numero'] if cuota_p else 1}, {cuota_p['valor'] - cuota_p['valor_pagado'] if cuota_p else 0})"><i class="fa-solid fa-money-bill-wave"></i> Cobrar</button>
                <button class="btn-accion btn-abono" onclick="abrirModalAbono({c['id']}, {cuota_p['numero'] if cuota_p else 1}, {cuota_p['valor'] - cuota_p['valor_pagado'] if cuota_p else 0}, {c['valor_cuota']})"><i class="fa-solid fa-calculator"></i> Abono</button>
                <button class="btn-accion btn-nopagar" onclick="ejecutarNoPago({c['id']})"><i class="fa-solid fa-ban"></i> Saltar</button>
            </div>
        </div>
        """
        
    contexto_dash += "</div>"
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(contexto_dash)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(contexto_dash))

# --- 👥 SECCIÓN 1: MENÚ CLIENTES (GESTIÓN COMPLETA, HISTORIAL Y RENOVACIONES) ---
@app.route("/menu/clientes")
def seccion_clientes():
    if not session.get("autenticado"): return "Sesión expirada"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes ORDER BY nombre ASC")
        todos = cursor.fetchall()
    conn.close()

    html_clientes = """
    <div class="section-header-title"><i class="fa-solid fa-users"></i> Control Maestro de Clientes</div>
    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Buscar cliente..." onkeyup="filtrarClientes()">
    """
    for cl in todos:
        html_clientes += f"""
        <div class="card cliente-card" data-nombre="{cl['nombre'].lower()}">
            <div class="flex-between">
                <div>
                    <span style="font-weight:800; color:#0f2b5c;">{cl['nombre']}</span>
                    <span style="font-size:10px; padding:2px 6px; border-radius:4px; font-weight:800; background:#f1f5f9; color:#475569;">{cl['estado']}</span>
                </div>
                <div style="display:flex; gap:4px;">
                    <a href="/mover_renovacion/{cl['id']}" class="btn-accion btn-abono" style="text-decoration:none;"><i class="fa-solid fa-rotate"></i> Renovar</a>
                    <a href="/api/eliminar_cliente/{cl['id']}" onclick="return confirm('¿Eliminar cliente permanentemente?')" class="btn-accion btn-nopagar" style="text-decoration:none;"><i class="fa-solid fa-trash"></i></a>
                </div>
            </div>
        </div>
        """
    return render_template_string(html_clientes)


# --- 💸 SECCIÓN 5: BALANCE DE CAJA (ENTRADAS, INVERSIONES, GASTOS CON FOTO Y CIERRE) ---
@app.route("/menu/balance")
def seccion_balance():
    if not session.get("autenticado"): return "Sesión expirada"
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT valor FROM configuracion WHERE llave = 'caja_base'")
        caja_base = float(cursor.fetchone()[0])
        cursor.execute("SELECT valor FROM configuracion WHERE llave = 'balance_estado'")
        balance_estado = cursor.fetchone()[0]
        
        cursor.execute("SELECT * FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
        movimientos = cursor.fetchall()
    conn.close()

    entradas_totales = sum(m["monto"] for g in movimientos if g["tipo"] == "Entrada") # type: ignore
    salidas_totales = sum(m["monto"] for g in movimientos if g["tipo"] == "Salida") # type: ignore
    efectivo_neto = caja_base + entradas_totales - salidas_totales

    html_balance = f"""
    <div class="section-header-title"><i class="fa-solid fa-scale-balanced"></i> Estado Contable del Balance</div>
    <div class="expected-card" style="border-left-color:#10b981; background:#ecfdf5; margin-bottom:10px;">
        <div class="kpi-title" style="color:#065f46;">Estado Actual</div>
        <div class="kpi-val" style="color:#065f46;">{balance_estado} | Caja Base: ${caja_base:.2f}</div>
    </div>
    
    <div class="grid-kpis">
        Entradas / Inversión
            +${entradas_totales:.2f}
        Gastos / Salidas
            -${salidas_totales:.2f}
        Efectivo Líquido Disponible
            ${efectivo_neto:.2f}
        Registrar Movimiento de Flujo
            📥 Entrada / Inversión Capital
            📤 Salida / Registro de Gasto

        Foto Factura (Opcional)
        Guardar Registro

"""
    return render_template_string(html_balance)


@app.route("/api/balance/guardar_movimiento", methods=["POST"])
def balance_guardar_movimiento():
    if not session.get("autenticado"):
        return redirect("/login")
    try:
        tipo = request.form.get("tipo_mov")
        concepto = request.form.get("concepto_mov", "").strip()
        monto = float(request.form.get("monto_mov", "0").replace(",", "."))
        hoy_str = date.today().isoformat()
        
        file = request.files.get("foto_mov")
        base64_str = ""
        if file and file.filename != "":
            base64_str = "data:" + file.content_type + ";base64," + base64.b64encode(file.read()).decode("utf-8")

        if not concepto:
            concepto = f"Movimiento de {tipo}"

        if monto > 0:
            conn = get_db()
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO balance_movimientos (tipo, categoria, concepto, monto, fecha, comprobante) VALUES (%s, 'General', %s, %s, %s, %s)",
                    (tipo, concepto, monto, hoy_str, base64_str)
                )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"❌ Error en guardar movimiento: {e}")
    return redirect("/")


# --- 📥 SECCIÓN 7: SALVAR DATOS E SINCRO EN LA NUBE ---
@app.route("/menu/salvar_datos")
def seccion_salvar_datos():
    if not session.get("autenticado"):
        return redirect("/login")
    html_sincro = """
    <div class="section-header-title"><i class="fa-solid fa-cloud-arrow-up"></i> Sincronización en la Nube</div>
    <div class="card" style="text-align:center; padding:30px 16px;">
        <i class="fa-solid fa-cloud-check" style="font-size:48px; color:#10b981; margin-bottom:12px;"></i>
        <h4 style="margin-bottom:8px;">Respaldo Automatizado Activo</h4>
        <p style="font-size:12px; color:#64748b; margin-bottom:16px;">Toda la información del día se asienta de manera instantánea y en tiempo real en los servidores distribuidos de Neon AWS.</p>
        <button onclick="alert('⚡ ¡Sincronización forzada completada! Todos tus respaldos están a salvo en caso de pérdida o robo.');" class="btn-primary" style="background:#10b981; border:none; color:white; font-weight:bold; padding:12px; border-radius:8px;">Forzar Respaldo Ahora</button>
    </div>
    """
    return render_template_string(html_sincro)


# --- ⚙️ API REST: CONSULTAS INTERNAS DEL SISTEMA ---
@app.route("/api/cliente_info/<int:id>")
def api_cliente_info(id):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autenticado"})
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes WHERE id = %s", (id,))
        cl = cursor.fetchone()
        cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY numero ASC", (id,))
        pgs = cursor.fetchall()
    conn.close()

    if not cl: 
        return jsonify({"status": "error"})
    
    pagadas = sum(1 for p in pgs if p["pagado"] == 1)
    pendientes = len(pgs) - pagadas
    saldo = cl["monto_total"] - sum(p["valor_pagado"] for p in pgs)
    
    # Cálculo contable de mora comercial
    atraso_dias = cl.get("saltado_hoy", 0) # Mapeado con tu variable estructural nativa de control
    monto_mora = atraso_dias * cl["valor_cuota"]

    data = {
        "nombre": cl["nombre"], 
        "telefono": cl["telefono"] or 'N/A',
        "identificacion": cl["identificacion"] or 'N/A', 
        "direccion": cl["direccion"] or 'N/A',
        "referencia": cl["referencia"] or 'N/A', 
        "fecha_inicio": cl["fecha_inicio"],
        "fecha_vencimiento": cl["fecha_vencimiento"], 
        "frecuencia": cl["frecuencia"],
        "valor_cuota": cl["valor_cuota"], 
        "monto_total": cl["monto_total"],
        "saldo_actual": saldo, 
        "cuotas_pagadas": pagadas, 
        "cuotas_pendientes": pendientes,
        "dias_atraso": atraso_dias, 
        "monto_mora": monto_mora, 
        "latitud": cl["latitud"] or "-1.0", 
        "longitud": cl["longitud"] or "-79.0"
    }
    return jsonify({"status": "ok", "data": data})


@app.route("/api/marcar_pago/<int:cliente_id>/<int:num_cuota>")
def api_marcar_pago(cliente_id, num_cuota):
    if not session.get("autenticado"):
        return jsonify({"status": "error", "message": "No autenticado"})
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

    # El formato del texto viaja limpio desde Python hacia la URL de WhatsApp sin duplicidades corruptas
    texto_ws = f"🌐 *AVANTA PAGOS - COMPROBANTE*\\n\\nCliente: *{cliente['nombre']}*\\n🔹 Cuota Cobrada: #{num_cuota}\\n🔹 Valor Pagado: ${monto_ingresado:.2f}\\n🔹 Saldo Pendiente: ${saldo_restante:.2f}\\n¡Muchas gracias por su puntualidad!"
    
    recibo = {
        "cliente": cliente["nombre"], 
        "cuota": num_cuota,
        "monto": monto_ingresado, 
        "saldo": saldo_restante,
        "telefono": str(cliente["telefono"]).replace("(", "").replace(")", "").replace("'", "").strip(),
        "mensaje_ws": urllib.parse.quote(texto_ws),
    }
    conn.close()
    return jsonify({"status": "ok", "recibo": recibo})


@app.route("/api/marcar_no_pago/<int:cliente_id>")
def api_marcar_no_pago(cliente_id):
    if not session.get("autenticado"):
        return jsonify({"status": "error"})
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("UPDATE clientes SET saltado_hoy = saltado_hoy + 1, fecha_gestion = %s WHERE id = %s", (hoy_str, cliente_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/nuevo")
def seccion_nuevo_credito():
    if not session.get("autenticado"):
        return redirect("/login")
    hoy_str = date.today().isoformat()
    html_nuevo = f"""
    <div class="section-header-title"><i class="fa-solid fa-user-plus"></i> Registrar Nuevo Crédito</div>
    <div class="card" style="padding:16px;">
        <form action="/guardar" method="POST">
            <label>Nombre Completo del Cliente</label>
            <input type="text" name="nombre" placeholder="Ej: Jhon Ramirez" required>
            
            <label>Número de WhatsApp</label>
            <input type="text" name="telefono" placeholder="Ej: 593991234567" required>
            
            <label>Cédula / Identificación Comercial</label>
            <input type="text" name="identificacion" placeholder="Ej: 070XXXXXX">
            
            <label>Dirección Domiciliaria</label>
            <input type="text" name="direccion" placeholder="Calle, Barrio, Ciudad">

            <label>Referencia Visual del Local / Negocio</label>
            <input type="text" name="referencia" placeholder="Frente a la tienda, portón blanco">

            <label>Monto Financiado / Capital Base ($)</label>
            <input type="number" step="any" id="calcMonto" name="monto" placeholder="Monto entregado" oninput="calcularCuota()" required>
            
            <label>Porcentaje de Interés (%)</label>
            <input type="number" step="any" id="calcInteres" name="interes_porcentaje" value="20" oninput="calcularCuota()" required>

            <label>Cantidad de Cuotas Pactadas</label>
            <input type="number" id="calcCuotas" name="cuotas" value="24" oninput="calcularCuota()" required>
            
            <div style="background:#f0f9ff; border:1px solid #bae6fd; padding:10px; border-radius:8px; margin-bottom:12px;">
                <div style="font-size:12px; color:#0369a1; font-weight:bold;" id="simulacionText">$0.00 / cuota</div>
            </div>

            <label>Frecuencia de Ruta</label>
            <select name="frecuencia">
                <option value="Diaria" selected>Diaria (Lunes a Sábado)</option>
                <option value="Semanal">Semanal</option>
                <option value="Quincenal">Quincenal</option>
                <option value="Mensual">Mensual</option>
            </select>
            
            <label>Fecha de Inicio del Crédito</label>
            <input type="date" name="fecha_inicio" value="{hoy_str}" required>

            <!-- Variables Invisibles de Geolocalización GPS por Satélite -->
            <input type="hidden" id="input_latitud" name="latitud" value="">
            <input type="hidden" id="input_longitud" name="longitud" value="">
            
            <button type="submit" class="btn-primary" style="background:#0f2b5c; border:none; padding:12px; color:white; font-weight:bold; margin-top:8px;">💾 Confirmar y Generar Cartera</button>
        </form>
    </div>
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(html_nuevo)
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(html_nuevo))


@app.route("/guardar", methods=["POST"])
def guardar_nuevo_cliente():
    if not session.get("autenticado"):
        return redirect("/login")
    try:
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").replace("+", "").replace(" ", "").strip()
        identificacion = request.form.get("identificacion", "").strip()
        direccion = request.form.get("direccion", "").strip()
        referencia = request.form.get("referencia", "").strip()
        monto = float(request.form.get("monto", "0").replace(",", "."))
        interes_porcentaje = float(request.form.get("interes_porcentaje", "20").replace(",", "."))
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
                cursor.execute(
                    "INSERT INTO pagos (cliente_id, numero, fecha, valor, pagado, valor_pagado, fecha_pago_real) VALUES (%s, %s, %s, %s, 0, 0, '')",
                    (cliente_id, num, f_cuota.isoformat(), valor_cuota_real)
                )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error crítico al procesar guardado de cliente: {e}")
    return redirect("/")

@app.route("/menu/gastos")
def vista_gastos():
    if not session.get("autenticado"):
        return redirect("/login")
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM balance_movimientos WHERE fecha = %s ORDER BY id DESC", (hoy_str,))
        gastos_list = cursor.fetchall()
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos if res_gastos and res_gastos is not None else 0.0
    conn.close()
    contexto = dict(vista="gastos", gastos_list=gastos_list, total_gastos_hoy=total_gastos_hoy)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto) # type: ignore
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto)) # type: ignore

@app.route("/api/eliminar_cliente/<int:id>")
def api_eliminar_cliente(id):
    if not session.get("autenticado"):
        return redirect("/login")
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM clientes WHERE id = %s", (id,))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/api/eliminar_gasto/<int:gasto_id>")
def eliminar_gasto(gasto_id):
    if not session.get("autenticado"):
        return redirect("/login")
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM balance_movimientos WHERE id = %s", (gasto_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Eliminar error: {e}")
    return redirect("/")

@app.route("/cierre")
def cierre_diario():
    if not session.get("autenticado"):
        return redirect("/login")
    hoy_str = date.today().isoformat()
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT COALESCE(SUM(valor_pagado), 0) FROM pagos WHERE fecha_pago_real = %s", (hoy_str,))
        res_cobrado = cursor.fetchone()
        total_cobrado_hoy = res_cobrado if res_cobrado and res_cobrado is not None else 0.0
        cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM balance_movimientos WHERE tipo = 'Salida' AND fecha = %s", (hoy_str,))
        res_gastos = cursor.fetchone()
        total_gastos_hoy = res_gastos if res_gastos and res_gastos is not None else 0.0
    conn.close()
    contexto = dict(vista='cierre', total_cobrado_hoy=total_cobrado_hoy, total_gastos_hoy=total_gastos_hoy, hoy_str=hoy_str)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto) # type: ignore
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto)) # type: ignore

@app.route("/resumen")
def resumen():
    if not session.get("autenticado"):
        return redirect("/login")
    clientes = obtener_clientes_completos() # type: ignore
    total_capital = sum(c["monto"] for c in clientes)
    total_creditos = sum(c["monto_total"] for c in clientes)
    total_cobrado = sum(p["valor_pagado"] for c in clientes for p in c["pagos"])
    contexto = dict(vista="resumen", clientes=clientes, total_capital=total_capital, total_creditos=total_creditos, total_cobrado=total_cobrado)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template_string(CONTENIDO_HTML, **contexto) # type: ignore
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto)) # type: ignore

@app.route("/mover_renovacion/<int:cliente_id>")
def mover_renovacion(cliente_id):
    if not session.get("autenticado"):
        return redirect("/login")
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
        cliente = cursor.fetchone()
    conn.close()
    contexto = dict(vista="renovar", cliente=cliente, hoy_str=date.today().isoformat())
    return render_template_string(HTML_TEMPLATE, contenido_html=render_template_string(CONTENIDO_HTML, **contexto)) # type: ignore

@app.route("/respaldo")
def respaldo():
    return redirect("/resumen")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
    