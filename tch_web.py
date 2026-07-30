import flet as ft
import os
import re
import json
import threading
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import psycopg2
import psycopg2.extras
import pdfplumber

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

COLOR_AZUL = "#185FA5"
COLOR_ROJO = "#A32D2D"

UBICACIONES_OBD2 = {
    "volkswagen": "Bajo el volante, a la izquierda del pedal de freno.",
    "audi": "Bajo el volante, junto al capó de apertura.",
    "seat": "Bajo el volante, cerca del fusible.",
    "skoda": "Bajo el volante, a la izquierda de la columna de dirección.",
    "bmw": "Bajo el volante, junto al panel de fusibles del lado del conductor.",
    "mercedes": "Bajo el volante o en la consola central baja, según modelo.",
    "opel": "Bajo el volante, cerca del pedal de freno.",
    "ford": "Bajo el volante, a la izquierda de la columna de dirección.",
    "renault": "Bajo el volante o en la consola central, según modelo.",
    "peugeot": "Bajo el volante, cerca del reposapiés del conductor.",
    "citroen": "Bajo el volante, cerca del reposapiés del conductor.",
    "fiat": "Bajo el volante, junto al panel de fusibles.",
    "toyota": "Bajo el volante, a la izquierda de la columna de dirección.",
    "nissan": "Bajo el volante, cerca del capó de apertura.",
    "volvo": "Bajo el volante, junto al pedal de freno.",
    "iveco": "Bajo el salpicadero, cerca del asiento del conductor (varía en camiones).",
    "man": "Cabina, bajo el salpicadero del lado del conductor (varía en camiones).",
    "scania": "Cabina, bajo el salpicadero del lado del conductor (varía en camiones).",
    "daf": "Cabina, bajo el salpicadero del lado del conductor (varía en camiones).",
}

FALLOS_ITV = {
    "frenos": "Revisar pastillas/discos de freno y purgar el circuito hidráulico.",
    "luces": "Comprobar bombillas, regulación de faros y conexiones eléctricas.",
    "neumaticos": "Sustituir neumáticos con desgaste irregular o profundidad de dibujo insuficiente.",
    "amortiguadores": "Revisar y sustituir amortiguadores desgastados.",
    "direccion": "Revisar holguras en la dirección y rótulas.",
    "emisiones": "Revisar sistema de escape y realizar puesta a punto del motor.",
    "chasis": "Revisar corrosión o daños estructurales en el chasis.",
    "ruido": "Revisar escape y elementos sueltos que generen ruido excesivo.",
    "fugas": "Localizar y reparar fugas de aceite o líquidos.",
    "cinturones": "Revisar anclajes y funcionamiento de los cinturones de seguridad.",
}

def obtener_ubicacion_obd2(marca):
    if not marca:
        return "Selecciona un vehículo para ver la ubicación habitual del conector."
    clave = marca.strip().lower()
    return UBICACIONES_OBD2.get(clave, f"Ubicación no registrada para '{marca}'. Suele estar bajo el volante, cerca del pedal de freno.")

def extraer_fallos_texto(texto):
    t = texto.lower()
    encontrados = []
    for clave, solucion in FALLOS_ITV.items():
        if clave in t:
            encontrados.append((clave.capitalize(), solucion))
    return encontrados

def formatear_faltas(encontrados):
    if not encontrados:
        return ""
    return "; ".join(f"{nombre}: {solucion}" for nombre, solucion in encontrados)

refrescadores_vehiculos = []

def notificar_cambio_vehiculos():
    for cb in refrescadores_vehiculos:
        cb()

# ---------- Conexión ----------

def conectar():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = conectar()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vehiculos (
        id SERIAL PRIMARY KEY, matricula TEXT UNIQUE, tipo TEXT, marca TEXT, modelo TEXT,
        intervalo_revision_km INTEGER, km_ultima_revision INTEGER, km_actual INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS mantenimiento (
        id SERIAL PRIMARY KEY, vehiculo_id INTEGER REFERENCES vehiculos(id) ON DELETE CASCADE,
        fecha TEXT, tipo TEXT, descripcion TEXT, costo REAL, kilometros INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS recambios (
        id SERIAL PRIMARY KEY, vehiculo_id INTEGER REFERENCES vehiculos(id) ON DELETE CASCADE,
        nombre TEXT, cantidad INTEGER, costo REAL, fecha TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS itv (
        id SERIAL PRIMARY KEY, vehiculo_id INTEGER REFERENCES vehiculos(id) ON DELETE CASCADE,
        fecha_inspeccion TEXT, fecha_vencimiento TEXT, faltas TEXT, matricula_detectada TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS diagnosticos (
        id SERIAL PRIMARY KEY, vehiculo_id INTEGER REFERENCES vehiculos(id) ON DELETE CASCADE,
        fecha TEXT, descripcion TEXT, codigos TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS configuracion (
        clave TEXT PRIMARY KEY, valor TEXT)''')
    conn.commit()
    c.close()
    conn.close()

def get_config(clave, default=""):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT valor FROM configuracion WHERE clave=%s", (clave,))
    row = c.fetchone()
    c.close()
    conn.close()
    return row[0] if row else default

def set_config(clave, valor):
    conn = conectar()
    c = conn.cursor()
    c.execute("""INSERT INTO configuracion (clave, valor) VALUES (%s, %s)
                 ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor""", (clave, valor))
    conn.commit()
    c.close()
    conn.close()

def entero_seguro(texto, permitir_vacio=True):
    if texto is None or not texto.strip():
        if permitir_vacio:
            return None, None
        return None, "Este campo es obligatorio."
    try:
        return int(texto.strip()), None
    except ValueError:
        return None, f"'{texto}' no es un número válido. Usa solo dígitos."

# ---------- Vehículos ----------

def get_vehiculos():
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT id, matricula, tipo, marca, modelo FROM vehiculos ORDER BY matricula")
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def add_vehiculo(matricula, tipo, marca, modelo):
    conn = conectar()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO vehiculos (matricula, tipo, marca, modelo) VALUES (%s, %s, %s, %s)",
                  (matricula, tipo, marca, modelo))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        c.close()
        conn.close()

def update_vehiculo(vid, matricula, tipo, marca, modelo):
    conn = conectar()
    c = conn.cursor()
    c.execute("UPDATE vehiculos SET matricula=%s, tipo=%s, marca=%s, modelo=%s WHERE id=%s",
              (matricula, tipo, marca, modelo, vid))
    conn.commit()
    c.close()
    conn.close()

def delete_vehiculo(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("DELETE FROM vehiculos WHERE id=%s", (vehiculo_id,))
    conn.commit()
    c.close()
    conn.close()

# ---------- Mantenimiento / km ----------

def actualizar_km_actual(vehiculo_id, km):
    if km is None:
        return
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT km_actual FROM vehiculos WHERE id=%s", (vehiculo_id,))
    row = c.fetchone()
    actual = row[0] if row and row[0] is not None else 0
    if km > actual:
        c.execute("UPDATE vehiculos SET km_actual=%s WHERE id=%s", (km, vehiculo_id))
        conn.commit()
    c.close()
    conn.close()

def get_info_revision(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT intervalo_revision_km, km_ultima_revision, km_actual FROM vehiculos WHERE id=%s", (vehiculo_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    return row if row else (None, None, None)

def update_intervalo_revision(vehiculo_id, intervalo):
    conn = conectar()
    c = conn.cursor()
    c.execute("UPDATE vehiculos SET intervalo_revision_km=%s WHERE id=%s", (intervalo, vehiculo_id))
    conn.commit()
    c.close()
    conn.close()

def marcar_revision_hecha(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT km_actual FROM vehiculos WHERE id=%s", (vehiculo_id,))
    row = c.fetchone()
    km_actual = row[0] if row else None
    c.execute("UPDATE vehiculos SET km_ultima_revision=%s WHERE id=%s", (km_actual, vehiculo_id))
    conn.commit()
    c.close()
    conn.close()

def add_mantenimiento(vehiculo_id, tipo, descripcion, kilometros=None, costo=0):
    conn = conectar()
    c = conn.cursor()
    c.execute("INSERT INTO mantenimiento (vehiculo_id, fecha, tipo, descripcion, costo, kilometros) VALUES (%s,%s,%s,%s,%s,%s)",
              (vehiculo_id, datetime.now().strftime("%Y-%m-%d"), tipo, descripcion, costo, kilometros))
    conn.commit()
    c.close()
    conn.close()
    actualizar_km_actual(vehiculo_id, kilometros)

def get_mantenimiento(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT id, fecha, tipo, descripcion, kilometros FROM mantenimiento WHERE vehiculo_id=%s ORDER BY fecha DESC", (vehiculo_id,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def update_mantenimiento(registro_id, tipo, descripcion, kilometros):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT vehiculo_id FROM mantenimiento WHERE id=%s", (registro_id,))
    row = c.fetchone()
    c.execute("UPDATE mantenimiento SET tipo=%s, descripcion=%s, kilometros=%s WHERE id=%s",
              (tipo, descripcion, kilometros, registro_id))
    conn.commit()
    c.close()
    conn.close()
    if row:
        actualizar_km_actual(row[0], kilometros)

def delete_mantenimiento(registro_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("DELETE FROM mantenimiento WHERE id=%s", (registro_id,))
    conn.commit()
    c.close()
    conn.close()

# ---------- Recambios ----------

def add_recambio(vehiculo_id, nombre, cantidad, costo=0):
    conn = conectar()
    c = conn.cursor()
    c.execute("INSERT INTO recambios (vehiculo_id, nombre, cantidad, costo, fecha) VALUES (%s,%s,%s,%s,%s)",
              (vehiculo_id, nombre, cantidad, costo, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    c.close()
    conn.close()

def add_recambio_exacto(vehiculo_id, nombre, cantidad, fecha, costo=0):
    conn = conectar()
    c = conn.cursor()
    c.execute("INSERT INTO recambios (vehiculo_id, nombre, cantidad, costo, fecha) VALUES (%s,%s,%s,%s,%s)",
              (vehiculo_id, nombre, cantidad, costo, fecha))
    conn.commit()
    c.close()
    conn.close()

def get_recambios(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT id, nombre, cantidad, fecha FROM recambios WHERE vehiculo_id=%s ORDER BY fecha DESC", (vehiculo_id,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def update_recambio(registro_id, nombre, cantidad):
    conn = conectar()
    c = conn.cursor()
    c.execute("UPDATE recambios SET nombre=%s, cantidad=%s WHERE id=%s", (nombre, cantidad, registro_id))
    conn.commit()
    c.close()
    conn.close()

def delete_recambio(registro_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("DELETE FROM recambios WHERE id=%s", (registro_id,))
    conn.commit()
    c.close()
    conn.close()

# ---------- ITV ----------

def extraer_texto_documento(ruta):
    ext = os.path.splitext(ruta)[1].lower()
    if ext == ".pdf":
        try:
            with pdfplumber.open(ruta) as pdf:
                texto = "\n".join([page.extract_text() or "" for page in pdf.pages])
            return texto, None
        except Exception as e:
            return "", f"No se pudo leer el PDF: {e}"
    elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff"):
        try:
            import pytesseract
            from PIL import Image
            imagen = Image.open(ruta)
            try:
                texto = pytesseract.image_to_string(imagen, lang="spa")
            except Exception:
                texto = pytesseract.image_to_string(imagen)
            return texto, None
        except ImportError:
            return "", "El OCR de fotos no está disponible en este servidor. La foto no pudo procesarse automáticamente."
        except Exception as e:
            return "", f"No se pudo leer la foto: {e}"
    else:
        return "", "Formato de archivo no soportado."

def extraer_matricula(texto):
    t = texto.upper()
    patron = r'\b(\d{4}[\s\-]?[BCDFGHJKLMNPRSTVWXYZ]{3}|[A-Z]{1,2}[\s\-]?\d{4}[\s\-]?[A-Z]{1,2})\b'
    m = re.search(patron, t)
    if m:
        return re.sub(r'[\s\-]', '', m.group(0))
    compacto = re.sub(r'[\s\-]+', '', t)
    m2 = re.search(r'(\d{4}[BCDFGHJKLMNPRSTVWXYZ]{3}|[A-Z]{1,2}\d{4}[A-Z]{1,2})', compacto)
    return m2.group(0) if m2 else None

def guardar_itv(vehiculo_id, faltas, matricula_detectada=None):
    conn = conectar()
    c = conn.cursor()
    fecha_insp = datetime.now().strftime("%Y-%m-%d")
    fecha_venc = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    c.execute("INSERT INTO itv (vehiculo_id, fecha_inspeccion, fecha_vencimiento, faltas, matricula_detectada) VALUES (%s,%s,%s,%s,%s)",
              (vehiculo_id, fecha_insp, fecha_venc, faltas, matricula_detectada))
    conn.commit()
    c.close()
    conn.close()

def add_itv_documento(vehiculo_id, ruta):
    texto, error = extraer_texto_documento(ruta)
    encontrados = extraer_fallos_texto(texto) if texto else []
    faltas = formatear_faltas(encontrados)
    matricula_detectada = extraer_matricula(texto) if texto else None
    guardar_itv(vehiculo_id, faltas, matricula_detectada)
    fragmento = texto[:400] if texto else ""
    return faltas, matricula_detectada, error, fragmento

def get_itv(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT id, fecha_inspeccion, fecha_vencimiento, faltas, matricula_detectada FROM itv WHERE vehiculo_id=%s ORDER BY fecha_inspeccion DESC", (vehiculo_id,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def update_itv(registro_id, fecha_vencimiento, faltas):
    conn = conectar()
    c = conn.cursor()
    c.execute("UPDATE itv SET fecha_vencimiento=%s, faltas=%s WHERE id=%s", (fecha_vencimiento, faltas, registro_id))
    conn.commit()
    c.close()
    conn.close()

def delete_itv(registro_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("DELETE FROM itv WHERE id=%s", (registro_id,))
    conn.commit()
    c.close()
    conn.close()

# ---------- Diagnósticos ----------

def extraer_codigos_falla(texto):
    encontrados = re.findall(r'\b[PBCU][0-9]{4}\b', texto.upper())
    vistos = []
    for cod in encontrados:
        if cod not in vistos:
            vistos.append(cod)
    return vistos

def add_diagnostico(vehiculo_id, descripcion, codigos=None):
    conn = conectar()
    c = conn.cursor()
    codigos_str = ", ".join(codigos) if codigos else ""
    c.execute("INSERT INTO diagnosticos (vehiculo_id, fecha, descripcion, codigos) VALUES (%s,%s,%s,%s)",
              (vehiculo_id, datetime.now().strftime("%Y-%m-%d"), descripcion, codigos_str))
    conn.commit()
    c.close()
    conn.close()

def add_diagnostico_pdf(vehiculo_id, ruta):
    texto, _ = extraer_texto_documento(ruta)
    codigos = extraer_codigos_falla(texto) if texto else []
    descripcion = f"Diagnóstico: {len(codigos)} código(s) detectado(s)" if codigos else "Diagnóstico: sin códigos detectados"
    add_diagnostico(vehiculo_id, descripcion, codigos)
    return codigos

def get_diagnosticos(vehiculo_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("SELECT id, fecha, descripcion, codigos FROM diagnosticos WHERE vehiculo_id=%s ORDER BY fecha DESC", (vehiculo_id,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def update_diagnostico(registro_id, descripcion, codigos_str):
    conn = conectar()
    c = conn.cursor()
    c.execute("UPDATE diagnosticos SET descripcion=%s, codigos=%s WHERE id=%s", (descripcion, codigos_str, registro_id))
    conn.commit()
    c.close()
    conn.close()

def delete_diagnostico(registro_id):
    conn = conectar()
    c = conn.cursor()
    c.execute("DELETE FROM diagnosticos WHERE id=%s", (registro_id,))
    conn.commit()
    c.close()
    conn.close()

# ---------- Alertas ----------

def check_itv_expiration():
    conn = conectar()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""SELECT v.matricula, i.fecha_vencimiento FROM vehiculos v
                 JOIN itv i ON v.id = i.vehiculo_id WHERE i.fecha_vencimiento <= %s""", (today,))
    expired = c.fetchall()
    c.close()
    conn.close()
    email = get_config("email")
    if expired and email:
        send_alert(email, get_config("password"), f"ITV expirada: {[m[0] for m in expired]}")

def send_alert(email, password, message):
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(email, password)
        msg = MIMEMultipart()
        msg["From"] = email
        msg["To"] = email
        msg["Subject"] = "Alerta TCH"
        msg.attach(MIMEText(message))
        server.send_message(msg)
        server.quit()
    except Exception:
        pass

# ---------- UI helpers ----------

def confirmar_eliminar(page, mensaje, on_confirm):
    def cerrar(e):
        page.pop_dialog()

    def confirmar(e):
        page.pop_dialog()
        on_confirm()

    dlg = ft.AlertDialog(
        modal=True, title=ft.Text("Confirmar eliminación"), content=ft.Text(mensaje),
        actions=[
            ft.TextButton("Cancelar", on_click=cerrar),
            ft.TextButton("Eliminar", on_click=confirmar, style=ft.ButtonStyle(color=COLOR_ROJO)),
        ],
    )
    page.show_dialog(dlg)

def mostrar_editor(page, titulo, campos, on_guardar):
    text_fields = {}
    controles = []
    for key, label, valor in campos:
        tf = ft.TextField(label=label, value=str(valor) if valor is not None else "")
        text_fields[key] = tf
        controles.append(tf)

    def cerrar(e):
        page.pop_dialog()

    def guardar(e):
        valores = {k: tf.value for k, tf in text_fields.items()}
        page.pop_dialog()
        on_guardar(valores)

    dlg = ft.AlertDialog(
        modal=True, title=ft.Text(titulo), content=ft.Column(controles, tight=True, spacing=10, width=350),
        actions=[
            ft.TextButton("Cancelar", on_click=cerrar),
            ft.TextButton("Guardar", on_click=guardar, style=ft.ButtonStyle(color=COLOR_AZUL)),
        ],
    )
    page.show_dialog(dlg)

def mostrar_info_obd2(page):
    filas_pines = [
        ("2", "SAE J1850 bus +"), ("4", "Chassis ground"), ("5", "Signal ground"),
        ("6", "ISO 15765-4 (CAN high)"), ("7", "ISO 9141 (K-line)"), ("10", "SAE J1850 bus -"),
        ("14", "ISO 15765-4 (CAN low)"), ("15", "ISO 9141 (L-line)"), ("16", "Batería +12V"),
    ]
    filas = [ft.Row([ft.Text(pin, weight="bold", width=40), ft.Text(desc, size=13)]) for pin, desc in filas_pines]
    contenido = ft.Column([
        ft.Text("Conector OBD2 (SAE J1962) — 16 pines", weight="bold", size=14),
        ft.Text("Protocolo más común: bus CAN (pines 6 y 14).", size=12),
        ft.Divider(),
        ft.Column(filas, spacing=4),
        ft.Text("El pin 16 alimenta la batería, incluso con el contacto apagado.", size=12, italic=True),
    ], tight=True, spacing=8, width=380)
    dlg = ft.AlertDialog(modal=True, title=ft.Text("Referencia OBD2"), content=contenido,
                         actions=[ft.TextButton("Cerrar", on_click=lambda e: page.pop_dialog())])
    page.show_dialog(dlg)

def boton_primario(texto, on_click, icon=None):
    return ft.Button(texto, icon=icon, on_click=on_click, style=ft.ButtonStyle(bgcolor=COLOR_AZUL, color="white"))

def abrir_url(page, url):
    page.launch_url(url)

# ---------- Pestañas ----------
# (misma lógica de interfaz que la versión Android; solo cambia la capa de datos)

def build_tab_vehiculos(page):
    veh_list = ft.ListView(spacing=5, height=400)

    def refresh_list():
        veh_list.controls.clear()
        for vid, matricula, tipo, marca, modelo in get_vehiculos():
            row = ft.Row([
                ft.Text(f"{matricula} - {tipo} {marca} {modelo}", size=14, expand=True),
                ft.IconButton(ft.Icons.EDIT, icon_color=COLOR_AZUL,
                             on_click=lambda e, vid=vid, mat=matricula, typ=tipo, mar=marca, mod=modelo: editar_veh(vid, mat, typ, mar, mod)),
                ft.IconButton(ft.Icons.DELETE, icon_color=COLOR_ROJO,
                             on_click=lambda e, vid=vid, m=matricula: confirmar_eliminar(
                                 page, f"¿Eliminar el vehículo {m} y todo su historial?", lambda: delete_and_refresh(vid)))
            ])
            veh_list.controls.append(row)
        page.update()

    def delete_and_refresh(vid):
        delete_vehiculo(vid)
        refresh_list()
        notificar_cambio_vehiculos()

    def editar_veh(vid, mat, typ, mar, mod):
        def guardar(valores):
            update_vehiculo(vid, valores["matricula"], valores["tipo"], valores["marca"], valores["modelo"])
            refresh_list()
            notificar_cambio_vehiculos()
        mostrar_editor(page, "Editar vehículo", [
            ("matricula", "Matrícula", mat), ("tipo", "Tipo (Camion/Furgoneta/Remolque/Coche)", typ),
            ("marca", "Marca", mar), ("modelo", "Modelo", mod),
        ], guardar)

    def add_veh():
        mat = matricula_input.value.strip()
        typ = tipo_dropdown.value
        mar = marca_input.value.strip()
        mod = modelo_input.value.strip()
        if mat and typ and mar and mod:
            if add_vehiculo(mat, typ, mar, mod):
                matricula_input.value = ""
                tipo_dropdown.value = None
                marca_input.value = ""
                modelo_input.value = ""
                refresh_list()
                notificar_cambio_vehiculos()

    matricula_input = ft.TextField(label="Matrícula", width=150)
    tipo_dropdown = ft.Dropdown(label="Tipo", options=[
        ft.dropdown.Option("Camion"), ft.dropdown.Option("Furgoneta"),
        ft.dropdown.Option("Remolque"), ft.dropdown.Option("Coche"),
    ], width=140)
    marca_input = ft.TextField(label="Marca", width=150)
    modelo_input = ft.TextField(label="Modelo", width=150)

    refresh_list()

    return ft.Column([
        ft.Row([matricula_input, tipo_dropdown, marca_input, modelo_input,
                boton_primario("Agregar", lambda e: add_veh())], wrap=True),
        veh_list
    ], scroll=ft.ScrollMode.AUTO, expand=True)

def build_tab_mantenimiento(page):
    veh_list = ft.Dropdown(label="Vehículo", width=250)
    mant_list = ft.ListView(spacing=5, height=340)
    intervalo_input = ft.TextField(label="Revisar cada (km)", width=150)
    aviso_revision = ft.Text("", size=13, weight="bold")

    def refresh_vehicles():
        actual = veh_list.value
        veh_list.options = [ft.dropdown.Option(f"{m[1]}") for m in get_vehiculos()]
        if actual in [o.key for o in veh_list.options]:
            veh_list.value = actual
        page.update()

    refrescadores_vehiculos.append(refresh_vehicles)

    def obtener_vid_actual():
        vecs = get_vehiculos()
        return next((v[0] for v in vecs if v[1] == veh_list.value), None)

    def refresh_aviso():
        vid = obtener_vid_actual()
        if not vid:
            aviso_revision.value = ""
            intervalo_input.value = ""
            page.update()
            return
        intervalo, km_ultima, km_actual = get_info_revision(vid)
        intervalo_input.value = str(intervalo) if intervalo is not None else ""
        if not intervalo:
            aviso_revision.value = f"Km actual: {km_actual if km_actual is not None else 0} | Define el intervalo de revisión."
            aviso_revision.color = None
        else:
            base = km_ultima if km_ultima is not None else 0
            proxima = base + intervalo
            actual = km_actual if km_actual is not None else 0
            faltan = proxima - actual
            if faltan <= 0:
                aviso_revision.value = f"⚠ Toca revisión — km actual {actual}, tocaba a los {proxima} km."
                aviso_revision.color = COLOR_ROJO
            else:
                aviso_revision.value = f"Km actual: {actual} | Próxima revisión a los {proxima} km (faltan {faltan} km)."
                aviso_revision.color = COLOR_AZUL
        page.update()

    def refresh_mant():
        refresh_aviso()
        mant_list.controls.clear()
        vid = obtener_vid_actual()
        if vid:
            for mid, fecha, tipo, desc, km in get_mantenimiento(vid):
                km_texto = f" | {km} km" if km is not None else ""
                mant_list.controls.append(
                    ft.Row([
                        ft.Text(f"{fecha} | {tipo} | {desc}{km_texto}", size=12, expand=True),
                        ft.IconButton(ft.Icons.EDIT, icon_color=COLOR_AZUL,
                                     on_click=lambda e, mid=mid, t=tipo, d=desc, k=km: editar_mant(mid, t, d, k)),
                        ft.IconButton(ft.Icons.DELETE, icon_color=COLOR_ROJO,
                                     on_click=lambda e, mid=mid: confirmar_eliminar(
                                         page, "¿Eliminar este registro de mantenimiento?",
                                         lambda: (delete_mantenimiento(mid), refresh_mant())))
                    ])
                )
        page.update()

    def editar_mant(mid, tipo, desc, km):
        def guardar(valores):
            km_val, error = entero_seguro(valores["kilometros"])
            if error:
                mostrar_editor(page, "Editar mantenimiento", [
                    ("tipo", "Tipo", valores["tipo"]), ("descripcion", "Descripción", valores["descripcion"]),
                    ("kilometros", f"Kilómetros ({error})", valores["kilometros"]),
                ], guardar)
                return
            update_mantenimiento(mid, valores["tipo"], valores["descripcion"], km_val)
            refresh_mant()
        mostrar_editor(page, "Editar mantenimiento", [
            ("tipo", "Tipo", tipo), ("descripcion", "Descripción", desc), ("kilometros", "Kilómetros", km),
        ], guardar)

    def add_mant():
        vid = obtener_vid_actual()
        if vid and tipo_input.value and desc_input.value:
            km_val, error = entero_seguro(km_input.value)
            if error:
                aviso_revision.value = error
                aviso_revision.color = COLOR_ROJO
                page.update()
                return
            add_mantenimiento(vid, tipo_input.value, desc_input.value, km_val)
            tipo_input.value = ""
            desc_input.value = ""
            km_input.value = ""
            refresh_mant()

    def guardar_intervalo():
        vid = obtener_vid_actual()
        if vid and intervalo_input.value.strip():
            valor, error = entero_seguro(intervalo_input.value, permitir_vacio=False)
            if error:
                aviso_revision.value = error
                aviso_revision.color = COLOR_ROJO
                page.update()
                return
            update_intervalo_revision(vid, valor)
            refresh_aviso()

    def marcar_revision():
        vid = obtener_vid_actual()
        if vid:
            marcar_revision_hecha(vid)
            refresh_aviso()

    tipo_input = ft.TextField(label="Tipo", width=150)
    desc_input = ft.TextField(label="Descripción", width=250)
    km_input = ft.TextField(label="Kilómetros", width=120)

    refresh_vehicles()

    return ft.Column([
        ft.Row([veh_list, boton_primario("Cargar", lambda e: refresh_mant())], wrap=True),
        ft.Row([tipo_input, desc_input, km_input, boton_primario("Agregar", lambda e: add_mant())], wrap=True),
        ft.Row([intervalo_input, boton_primario("Guardar intervalo", lambda e: guardar_intervalo()),
                boton_primario("Marcar revisión realizada", lambda e: marcar_revision())], wrap=True),
        aviso_revision,
        mant_list
    ], scroll=ft.ScrollMode.AUTO, expand=True)

def build_tab_recambios(page):
    veh_list = ft.Dropdown(label="Vehículo", width=250)
    rec_list = ft.ListView(spacing=5, height=380)
    deshacer_row = ft.Row(visible=False)

    def refresh_vehicles():
        actual = veh_list.value
        veh_list.options = [ft.dropdown.Option(f"{m[1]}") for m in get_vehiculos()]
        if actual in [o.key for o in veh_list.options]:
            veh_list.value = actual
        page.update()

    refrescadores_vehiculos.append(refresh_vehicles)

    def ocultar_deshacer():
        deshacer_row.visible = False
        deshacer_row.controls = []

    def mostrar_deshacer(mensaje, accion_deshacer):
        deshacer_row.controls = [
            ft.Text(mensaje, size=12, italic=True, expand=True),
            ft.TextButton("Deshacer", on_click=lambda e: (accion_deshacer(), ocultar_deshacer(), refresh_rec())),
        ]
        deshacer_row.visible = True

    def refresh_rec():
        rec_list.controls.clear()
        if veh_list.value:
            vecs = get_vehiculos()
            vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
            if vid:
                for rid, nombre, cant, fecha in get_recambios(vid):
                    rec_list.controls.append(
                        ft.Row([
                            ft.Text(f"{fecha} | {nombre} x{cant}", size=12, expand=True),
                            ft.IconButton(ft.Icons.EDIT, icon_color=COLOR_AZUL,
                                         on_click=lambda e, rid=rid, n=nombre, ca=cant: editar_rec(rid, n, ca)),
                            ft.IconButton(ft.Icons.DELETE, icon_color=COLOR_ROJO,
                                         on_click=lambda e, rid=rid, vid=vid, n=nombre, ca=cant, f=fecha: eliminar_con_deshacer(rid, vid, n, ca, f))
                        ])
                    )
        page.update()

    def eliminar_con_deshacer(rid, vid, nombre, cantidad, fecha):
        def hacerlo():
            delete_recambio(rid)
            refresh_rec()
            mostrar_deshacer(f"Eliminado: {nombre} x{cantidad}.", lambda: add_recambio_exacto(vid, nombre, cantidad, fecha))
            page.update()
        confirmar_eliminar(page, "¿Eliminar este recambio?", hacerlo)

    def editar_rec(rid, nombre, cantidad):
        def guardar(valores):
            nombre_anterior, cantidad_anterior = nombre, cantidad
            cant_val, error = entero_seguro(valores["cantidad"], permitir_vacio=False)
            if error:
                mostrar_editor(page, "Editar recambio", [
                    ("nombre", "Nombre", valores["nombre"]), ("cantidad", f"Cantidad ({error})", valores["cantidad"]),
                ], guardar)
                return
            update_recambio(rid, valores["nombre"], cant_val)
            refresh_rec()
            mostrar_deshacer(
                f"Editado: {nombre_anterior} x{cantidad_anterior} → {valores['nombre']} x{cant_val}.",
                lambda: update_recambio(rid, nombre_anterior, int(cantidad_anterior))
            )
            page.update()
        mostrar_editor(page, "Editar recambio", [("nombre", "Nombre", nombre), ("cantidad", "Cantidad", cantidad)], guardar)

    def add_rec():
        if veh_list.value and nombre_input.value and cant_input.value:
            vecs = get_vehiculos()
            vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
            if vid:
                cant_val, error = entero_seguro(cant_input.value, permitir_vacio=False)
                if error:
                    deshacer_row.controls = [ft.Text(error, size=12, color=COLOR_ROJO)]
                    deshacer_row.visible = True
                    page.update()
                    return
                add_recambio(vid, nombre_input.value, cant_val)
                nombre_input.value = ""
                cant_input.value = ""
                ocultar_deshacer()
                refresh_rec()

    nombre_input = ft.TextField(label="Nombre", width=200)
    cant_input = ft.TextField(label="Cantidad", width=100)

    refresh_vehicles()

    return ft.Column([
        ft.Row([veh_list, boton_primario("Cargar", lambda e: refresh_rec())], wrap=True),
        ft.Row([nombre_input, cant_input, boton_primario("Agregar", lambda e: add_rec())], wrap=True),
        deshacer_row,
        rec_list
    ], scroll=ft.ScrollMode.AUTO, expand=True)

def build_tab_itv(page):
    veh_list = ft.Dropdown(label="Vehículo", width=250)
    itv_list = ft.ListView(spacing=8, height=340)
    status_text = ft.Text("", size=12)

    file_picker = ft.FilePicker()
    page.overlay.append(file_picker)

    def refresh_vehicles():
        actual = veh_list.value
        veh_list.options = [ft.dropdown.Option(f"{m[1]}") for m in get_vehiculos()]
        if actual in [o.key for o in veh_list.options]:
            veh_list.value = actual
        page.update()

    refrescadores_vehiculos.append(refresh_vehicles)

    def refresh_itv():
        itv_list.controls.clear()
        if veh_list.value:
            vecs = get_vehiculos()
            vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
            if vid:
                for iid, insp, venc, faltas, matricula_det in get_itv(vid):
                    encabezado = ft.Row([
                        ft.Text(f"{insp} → Vence: {venc}", size=13, weight="bold", expand=True),
                        ft.IconButton(ft.Icons.EDIT, icon_color=COLOR_AZUL,
                                     on_click=lambda e, iid=iid, v=venc, f=faltas: editar_itv(iid, v, f)),
                        ft.IconButton(ft.Icons.DELETE, icon_color=COLOR_ROJO,
                                     on_click=lambda e, iid=iid: confirmar_eliminar(
                                         page, "¿Eliminar este registro de ITV?", lambda: (delete_itv(iid), refresh_itv())))
                    ])
                    entrada = ft.Column([encabezado], spacing=2)
                    entrada.controls.append(ft.Text(faltas if faltas else "Sin defectos detectados", size=12))
                    if matricula_det:
                        vec_actual = next((v for v in vecs if v[0] == vid), None)
                        coincide = vec_actual and matricula_det == vec_actual[1].upper().replace(" ", "")
                        color_mat = "green" if coincide else COLOR_ROJO
                        texto_mat = f"Matrícula detectada: {matricula_det}" + ("" if coincide else " ⚠ no coincide")
                        entrada.controls.append(ft.Text(texto_mat, size=11, italic=True, color=color_mat))
                    itv_list.controls.append(entrada)
                    itv_list.controls.append(ft.Divider(height=1))
        page.update()

    def editar_itv(iid, venc, faltas):
        def guardar(valores):
            update_itv(iid, valores["fecha_vencimiento"], valores["faltas"])
            refresh_itv()
        mostrar_editor(page, "Editar ITV", [
            ("fecha_vencimiento", "Fecha vencimiento (YYYY-MM-DD)", venc), ("faltas", "Faltas", faltas or ""),
        ], guardar)

    def al_elegir_documento(e: ft.FilePickerResultEvent):
        if not e.files:
            return
        ruta = e.files[0].path
        if not ruta:
            status_text.value = "No se pudo acceder al archivo elegido."
            status_text.color = COLOR_ROJO
            page.update()
            return
        vecs = get_vehiculos()
        veh = next((v for v in vecs if v[1] == veh_list.value), None)
        if not veh:
            return
        vid = veh[0]
        faltas, matricula_detectada, error, fragmento = add_itv_documento(vid, ruta)
        if error:
            status_text.value = error
            status_text.color = "orange"
        elif faltas:
            status_text.value = f"Defectos detectados: {faltas}"
            status_text.color = COLOR_ROJO
        elif not matricula_detectada:
            status_text.value = f"No se detectaron defectos ni matrícula. Texto leído: {fragmento or '(vacío)'}"
            status_text.color = "orange"
        else:
            status_text.value = "Documento guardado. No se detectaron defectos."
            status_text.color = "green"
        refresh_itv()
        page.update()

    file_picker.on_result = al_elegir_documento

    def upload_documento():
        if not veh_list.value:
            status_text.value = "Selecciona un vehículo primero"
            status_text.color = COLOR_ROJO
            page.update()
            return
        file_picker.pick_files(dialog_title="Selecciona el documento de ITV", allow_multiple=False,
                               allowed_extensions=["pdf", "jpg", "jpeg", "png"])

    refresh_vehicles()

    return ft.Column([
        ft.Row([veh_list, boton_primario("Cargar", lambda e: refresh_itv()),
                boton_primario("Subir foto o PDF de ITV", lambda e: upload_documento(), icon=ft.Icons.UPLOAD_FILE)], wrap=True),
        status_text,
        itv_list
    ], scroll=ft.ScrollMode.AUTO, expand=True)

def build_tab_diagnosticos(page):
    veh_list = ft.Dropdown(label="Vehículo", width=250)
    diag_list = ft.ListView(spacing=8, height=300)
    desc_input = ft.TextField(label="Descripción", width=300)
    status_text = ft.Text("", size=12, color="green")
    ubicacion_texto = ft.Text("", size=12, italic=True)
    pieza_input = ft.TextField(label="ECU / pieza a buscar", width=220)
    pieza_status = ft.Text("", size=12)

    file_picker = ft.FilePicker()
    page.overlay.append(file_picker)

    def refresh_vehicles():
        actual = veh_list.value
        veh_list.options = [ft.dropdown.Option(f"{m[1]}") for m in get_vehiculos()]
        if actual in [o.key for o in veh_list.options]:
            veh_list.value = actual
        page.update()

    refrescadores_vehiculos.append(refresh_vehicles)

    def buscar_causa(codigo):
        abrir_url(page, f"https://www.google.com/search?q=codigo+de+falla+{codigo}+causas+solucion")

    def buscar_ubicacion(codigo):
        abrir_url(page, f"https://www.google.com/search?q=codigo+{codigo}+ubicacion+sensor+componente+conector")

    def buscar_pieza():
        vecs = get_vehiculos()
        veh = next((v for v in vecs if v[1] == veh_list.value), None)
        if not veh:
            pieza_status.value = "Selecciona un vehículo primero"
            pieza_status.color = COLOR_ROJO
            page.update()
            return
        if not pieza_input.value.strip():
            pieza_status.value = "Escribe la ECU o pieza a buscar"
            pieza_status.color = COLOR_ROJO
            page.update()
            return
        marca, modelo = veh[3], veh[4]
        pieza = pieza_input.value.strip()
        abrir_url(page, f"https://www.google.com/search?tbm=isch&q=ubicacion+{pieza}+{marca}+{modelo}")
        pieza_status.value = f"Buscando ubicación de '{pieza}' en {marca} {modelo}..."
        pieza_status.color = COLOR_AZUL
        page.update()

    def actualizar_ubicacion_obd2():
        vecs = get_vehiculos()
        veh = next((v for v in vecs if v[1] == veh_list.value), None)
        ubicacion_texto.value = f"Ubicación habitual en {veh[3]}: {obtener_ubicacion_obd2(veh[3])}" if veh else obtener_ubicacion_obd2(None)

    def refresh_diag():
        actualizar_ubicacion_obd2()
        diag_list.controls.clear()
        if veh_list.value:
            vecs = get_vehiculos()
            vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
            if vid:
                for did, fecha, desc, codigos in get_diagnosticos(vid):
                    encabezado = ft.Row([
                        ft.Text(f"{fecha} | {desc}", size=13, weight="bold", expand=True),
                        ft.IconButton(ft.Icons.EDIT, icon_color=COLOR_AZUL,
                                     on_click=lambda e, did=did, d=desc, c=codigos: editar_diag(did, d, c)),
                        ft.IconButton(ft.Icons.DELETE, icon_color=COLOR_ROJO,
                                     on_click=lambda e, did=did: confirmar_eliminar(
                                         page, "¿Eliminar este diagnóstico?", lambda: (delete_diagnostico(did), refresh_diag())))
                    ])
                    entrada = ft.Column([encabezado], spacing=2)
                    if codigos:
                        for cod in [c.strip() for c in codigos.split(",") if c.strip()]:
                            entrada.controls.append(
                                ft.Row([
                                    ft.Text(cod, size=13, weight="bold", width=60),
                                    ft.OutlinedButton("Causas", icon=ft.Icons.SEARCH, on_click=lambda e, c=cod: buscar_causa(c)),
                                    ft.OutlinedButton("Ubicación", icon=ft.Icons.PLACE, on_click=lambda e, c=cod: buscar_ubicacion(c)),
                                ], spacing=6, wrap=True)
                            )
                    diag_list.controls.append(entrada)
                    diag_list.controls.append(ft.Divider(height=1))
        page.update()

    def editar_diag(did, desc, codigos):
        def guardar(valores):
            update_diagnostico(did, valores["descripcion"], valores["codigos"])
            refresh_diag()
        mostrar_editor(page, "Editar diagnóstico", [
            ("descripcion", "Descripción", desc), ("codigos", "Códigos (separados por coma)", codigos or ""),
        ], guardar)

    def add_diag():
        if veh_list.value and desc_input.value:
            vecs = get_vehiculos()
            vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
            if vid:
                add_diagnostico(vid, desc_input.value)
                desc_input.value = ""
                refresh_diag()

    def al_elegir_pdf_diag(e: ft.FilePickerResultEvent):
        if not e.files or not veh_list.value:
            return
        ruta = e.files[0].path
        vecs = get_vehiculos()
        vid = next((v[0] for v in vecs if v[1] == veh_list.value), None)
        if vid and ruta:
            codigos = add_diagnostico_pdf(vid, ruta)
            status_text.value = f"Detectados: {', '.join(codigos)}" if codigos else "No se detectaron códigos de falla."
            status_text.color = COLOR_AZUL if codigos else "orange"
            refresh_diag()
            page.update()

    file_picker.on_result = al_elegir_pdf_diag

    def subir_pdf_autocom():
        if not veh_list.value:
            status_text.value = "Selecciona un vehículo primero"
            status_text.color = COLOR_ROJO
            page.update()
            return
        file_picker.pick_files(dialog_title="Selecciona el PDF de diagnóstico", allow_multiple=False, allowed_extensions=["pdf"])

    refresh_vehicles()

    return ft.Column([
        ft.Row([veh_list, boton_primario("Cargar", lambda e: refresh_diag())], wrap=True),
        ft.Row([pieza_input, boton_primario("Buscar ubicación de pieza/ECU", lambda e: buscar_pieza(), icon=ft.Icons.SEARCH)], wrap=True),
        pieza_status,
        ft.Row([desc_input, boton_primario("Agregar", lambda e: add_diag()),
                boton_primario("Subir PDF Autocom", lambda e: subir_pdf_autocom(), icon=ft.Icons.UPLOAD_FILE),
                ft.IconButton(ft.Icons.INFO_OUTLINE, icon_color=COLOR_AZUL, tooltip="Referencia OBD2",
                             on_click=lambda e: mostrar_info_obd2(page))], wrap=True),
        ubicacion_texto,
        status_text,
        diag_list
    ], scroll=ft.ScrollMode.AUTO, expand=True)

def build_tab_config(page):
    email_input = ft.TextField(label="Email", value=get_config("email"), width=280)
    password_input = ft.TextField(label="Contraseña", value=get_config("password"), password=True, width=280)
    export_status = ft.Text("", size=12)
    export_area = ft.TextField(label="Copia de seguridad (JSON)", multiline=True, min_lines=4, max_lines=8, read_only=True, visible=False)

    def save_cfg():
        set_config("email", email_input.value)
        set_config("password", password_input.value)

    def exportar():
        datos = {}
        conn = conectar()
        c = conn.cursor()
        for tabla in ["vehiculos", "mantenimiento", "recambios", "itv", "diagnosticos"]:
            c.execute(f"SELECT * FROM {tabla}")
            cols = [d[0] for d in c.description]
            datos[tabla] = [dict(zip(cols, fila)) for fila in c.fetchall()]
        c.close()
        conn.close()
        export_area.value = json.dumps(datos, ensure_ascii=False, indent=2, default=str)
        export_area.visible = True
        export_status.value = "Copia generada abajo. Selecciona todo el texto y guárdalo en un archivo .json."
        export_status.color = COLOR_AZUL
        page.update()

    return ft.Column([
        ft.Text("Configuración", size=16, weight="bold"),
        email_input,
        password_input,
        boton_primario("Guardar", lambda e: save_cfg()),
        ft.Divider(),
        ft.Text("Copia de seguridad", size=14, weight="bold"),
        ft.Text("Genera un JSON con todos tus datos para guardarlo tú mismo como copia.", size=12),
        boton_primario("Generar copia de seguridad", lambda e: exportar(), icon=ft.Icons.SAVE),
        export_status,
        export_area,
    ], spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)

def main(page: ft.Page):
    page.title = "TCH - Gestión de Mantenimiento"
    page.theme = ft.Theme(color_scheme_seed=COLOR_AZUL)
    init_db()
    refrescadores_vehiculos.clear()

    barra_titulo = ft.Container(
        content=ft.Row([
            ft.Text("T", size=20, weight="bold", italic=True, color="#2255CC"),
            ft.Text("C", size=20, weight="bold", italic=True, color="#CC2222"),
            ft.Text("H", size=20, weight="bold", italic=True, color="#CC2222"),
            ft.Text(" - Mantenimiento", size=15, weight="bold", color="white"),
        ], spacing=0),
        bgcolor=COLOR_AZUL,
        padding=ft.Padding(16, 12, 16, 12),
    )

    nombres_tabs = ["Vehículos", "Mantenimiento", "Recambios", "ITV", "Diagnósticos", "Config"]
    contenidos_tabs = [
        build_tab_vehiculos(page),
        build_tab_mantenimiento(page),
        build_tab_recambios(page),
        build_tab_itv(page),
        build_tab_diagnosticos(page),
        build_tab_config(page),
    ]

    tabs = ft.Tabs(
        length=len(nombres_tabs), selected_index=0, expand=True,
        content=ft.Column(expand=True, controls=[
            ft.TabBar(tabs=[ft.Tab(label=ft.Text(nombre)) for nombre in nombres_tabs]),
            ft.TabBarView(expand=True, controls=contenidos_tabs),
        ]),
    )

    marca_agua = ft.Container(
        content=ft.Image(src="tch.jpeg", width=280, fit="contain"),
        opacity=0.10, alignment=ft.Alignment.CENTER, expand=True,
    )

    page.add(ft.Column([barra_titulo, ft.Stack([marca_agua, tabs], expand=True)], expand=True, spacing=0))

    threading.Thread(target=check_itv_expiration, daemon=True).start()

PORT = int(os.environ.get("PORT", 8550))
ft.app(target=main, assets_dir="assets", host="0.0.0.0", port=PORT)
