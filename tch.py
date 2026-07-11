import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import sqlite3
import os
import re
import io
import json
import shutil
import base64
import smtplib
import threading
from email.mime.text import MIMEText
from datetime import date, datetime
from PIL import Image, ImageTk

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import requests
except Exception:
    requests = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tch.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DOCS_DIR = os.path.join(BASE_DIR, "documentos")
ITV_DIR = os.path.join(DOCS_DIR, "itv")
DIAG_DIR = os.path.join(DOCS_DIR, "diagnosis")

os.makedirs(ITV_DIR, exist_ok=True)
os.makedirs(DIAG_DIR, exist_ok=True)

TIPOS = ["Camion", "Furgoneta", "Remolque", "Coche"]

FALLO_KEYWORDS = [
    "defecto grave", "defecto leve", "defecto muy grave", "rechazo",
    "desfavorable", "no apto", "fallo", "anomalia", "anomalía",
    "deficiencia", "incumple", "sustituir", "reparar"
]

VENCIMIENTO_KEYWORDS = [
    "proxima itv", "próxima itv", "valido hasta", "válido hasta",
    "fecha limite", "fecha límite", "vencimiento", "caduca", "valida hasta", "válida hasta"
]

DATE_REGEX = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")

NAVY = "#1a3c8f"
RED = "#c0392b"
BG_LIGHT = "#f1f4f8"
CARD_BG = "#ffffff"
TEXT_DARK = "#1e2430"
TEXT_MUTED = "#6b7280"

DEFAULT_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "email_destino": "",
    "github_token": "",
    "github_repo": "",
    "github_branch": "main",
    "github_path": "tch_data.json"
}


def cargar_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def guardar_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def init_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mantenimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            matricula TEXT NOT NULL,
            fecha TEXT NOT NULL,
            kilometros INTEGER NOT NULL,
            mantenimiento TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS itv_documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            matricula TEXT NOT NULL,
            fecha TEXT NOT NULL,
            archivo TEXT NOT NULL,
            fallos TEXT,
            vencimiento TEXT,
            avisado INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS diagnosis_documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            matricula TEXT NOT NULL,
            fecha TEXT NOT NULL,
            archivo TEXT NOT NULL,
            notas TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recambios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            matricula TEXT NOT NULL,
            fecha TEXT NOT NULL,
            recambio TEXT NOT NULL,
            kilometros TEXT,
            notas TEXT
        )
    """)
    cur.execute("PRAGMA table_info(itv_documentos)")
    columnas_existentes = [c[1] for c in cur.fetchall()]
    if "vencimiento" not in columnas_existentes:
        cur.execute("ALTER TABLE itv_documentos ADD COLUMN vencimiento TEXT")
    if "avisado" not in columnas_existentes:
        cur.execute("ALTER TABLE itv_documentos ADD COLUMN avisado INTEGER DEFAULT 0")
    con.commit()
    con.close()


def extraer_texto_pdf(ruta):
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(ruta)
        texto = ""
        for page in reader.pages:
            texto += (page.extract_text() or "") + "\n"
        return texto
    except Exception:
        return ""


def detectar_fallos(texto):
    texto_low = texto.lower()
    encontrados = []
    for kw in FALLO_KEYWORDS:
        if kw in texto_low:
            for linea in texto.split("\n"):
                if kw in linea.lower() and linea.strip():
                    encontrados.append(linea.strip())
    vistos = set()
    resultado = []
    for l in encontrados:
        if l not in vistos:
            vistos.add(l)
            resultado.append(l)
    return resultado


def detectar_vencimiento(texto):
    lineas = texto.split("\n")
    texto_low = texto.lower()
    for kw in VENCIMIENTO_KEYWORDS:
        idx = texto_low.find(kw)
        if idx != -1:
            fragmento = texto[idx: idx + 120]
            m = DATE_REGEX.search(fragmento)
            if m:
                return _normalizar_fecha(m.group(1))
    m = DATE_REGEX.findall(texto)
    if m:
        return _normalizar_fecha(m[-1])
    return ""


def _normalizar_fecha(texto_fecha):
    texto_fecha = texto_fecha.replace("-", "/")
    partes = texto_fecha.split("/")
    if len(partes) == 3:
        d, mth, y = partes
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(d):02d}/{int(mth):02d}/{y}"
        except Exception:
            return texto_fecha
    return texto_fecha


def enviar_email(cfg, asunto, cuerpo):
    if not cfg.get("smtp_user") or not cfg.get("email_destino"):
        return False, "Configura el correo en la pestaña Ajustes."
    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = cfg["smtp_user"]
        msg["To"] = cfg["email_destino"]
        with smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]), timeout=15) as server:
            server.starttls()
            server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.sendmail(cfg["smtp_user"], [cfg["email_destino"]], msg.as_string())
        return True, "Correo enviado correctamente."
    except Exception as e:
        return False, f"Error al enviar correo: {e}"


def exportar_datos_json():
    con = sqlite3.connect(DB_PATH, timeout=10)
    cur = con.cursor()
    cur.execute("SELECT tipo, matricula, fecha, kilometros, mantenimiento FROM mantenimientos ORDER BY id DESC")
    mant = [dict(zip(["tipo", "matricula", "fecha", "kilometros", "mantenimiento"], r)) for r in cur.fetchall()]
    cur.execute("SELECT matricula, fecha, archivo, fallos, vencimiento FROM itv_documentos ORDER BY id DESC")
    itv = [dict(zip(["matricula", "fecha", "archivo", "fallos", "vencimiento"], r)) for r in cur.fetchall()]
    cur.execute("SELECT matricula, fecha, archivo, notas FROM diagnosis_documentos ORDER BY id DESC")
    diag = [dict(zip(["matricula", "fecha", "archivo", "notas"], r)) for r in cur.fetchall()]
    con.close()
    return {
        "actualizado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mantenimientos": mant,
        "itv": itv,
        "diagnosis": diag
    }


def sincronizar_github(cfg):
    if requests is None:
        return False, "Falta instalar la libreria 'requests'."
    token = cfg.get("github_token")
    repo = cfg.get("github_repo")
    branch = cfg.get("github_branch") or "main"
    path = cfg.get("github_path") or "tch_data.json"
    if not token or not repo:
        return False, "Configura el token y el repositorio de GitHub en Ajustes."

    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    contenido = json.dumps(exportar_datos_json(), ensure_ascii=False, indent=2)
    contenido_b64 = base64.b64encode(contenido.encode("utf-8")).decode("utf-8")

    sha = None
    try:
        r_get = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=15)
        if r_get.status_code == 200:
            sha = r_get.json().get("sha")
    except Exception as e:
        return False, f"Error consultando GitHub: {e}"

    payload = {
        "message": f"Actualizacion de datos TCH {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "content": contenido_b64,
        "branch": branch
    }
    if sha:
        payload["sha"] = sha

    try:
        r_put = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r_put.status_code in (200, 201):
            return True, "Datos sincronizados con GitHub correctamente."
        return False, f"Error de GitHub ({r_put.status_code}): {r_put.text[:200]}"
    except Exception as e:
        return False, f"Error de conexion: {e}"


LOGO_TCH_B64 = (
    "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"
)


def _logo_original():
    data = base64.b64decode(LOGO_TCH_B64)
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _logo_transparente():
    img = _logo_original()
    nueva = []
    for r, g, b, a in img.getdata():
        if r > 240 and g > 240 and b > 240:
            nueva.append((255, 255, 255, 0))
        else:
            nueva.append((r, g, b, a))
    img.putdata(nueva)
    return img


def generar_icono():
    return _logo_original()


def generar_logo_header(tamano=70):
    return _logo_transparente().resize((tamano, tamano), Image.LANCZOS)


def generar_fondo_marca_agua(ancho, alto):
    ancho = max(int(ancho), 1)
    alto = max(int(alto), 1)
    fondo = Image.new("RGBA", (ancho, alto), (241, 244, 248, 255))
    logo_t = _logo_transparente()
    lado = int(min(ancho, alto) * 0.8)
    if lado < 20:
        lado = 20
    marca = logo_t.resize((lado, lado), Image.LANCZOS)
    alpha = marca.split()[3].point(lambda p: int(p * 0.20))
    marca.putalpha(alpha)
    x = (ancho - lado) // 2
    y = (alto - lado) // 2
    fondo.paste(marca, (x, y), marca)
    return fondo.convert("RGB")


class Card(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=CARD_BG, highlightbackground="#dfe3e8",
                          highlightthickness=1, bd=0, **kwargs)


BOTON_ESTILOS = {
    "accent": dict(bg=RED, fg="white", activebackground="#a5311f", activeforeground="white",
                   font=("Segoe UI", 10, "bold")),
    "ghost": dict(bg=CARD_BG, fg=TEXT_DARK, activebackground="#f0f1f3", activeforeground=TEXT_DARK,
                  font=("Segoe UI", 10)),
    "danger": dict(bg="#fdecea", fg=RED, activebackground="#f8d3ce", activeforeground=RED,
                   font=("Segoe UI", 9, "bold")),
}


def crear_boton(parent, text, command, kind="accent"):
    cfg = BOTON_ESTILOS[kind]
    return tk.Button(parent, text=text, command=command, bg=cfg["bg"], fg=cfg["fg"],
                      activebackground=cfg["activebackground"], activeforeground=cfg["activeforeground"],
                      font=cfg["font"], relief="flat", bd=0, padx=16, pady=9, cursor="hand2")


class TCHApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TCH - Mantenimiento de Vehiculos")
        self.configure(bg=BG_LIGHT)
        self.config_data = cargar_config()

        try:
            self.state("zoomed")
        except Exception:
            self.attributes("-zoomed", True)
        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        try:
            self.icon_img = ImageTk.PhotoImage(generar_icono())
            self.iconphoto(True, self.icon_img)
        except Exception:
            pass

        # Fondo con marca de agua: un Label con la imagen ocupa toda la ventana
        # y actua como "master" de todo el contenido, de forma que el propio
        # Label muestra la imagen a traves de cualquier hueco/padding que dejen
        # los widgets hijos (a diferencia de un Frame opaco que la taparia).
        self.bg_photo = None
        self.container = tk.Label(self, bg=BG_LIGHT, bd=0)
        self.container.place(x=0, y=0, relwidth=1, relheight=1)
        self.container.bind("<Configure>", self._on_resize)

        self._build_style()
        self._build_ui()
        self._load_registros()
        self._load_recambios()
        self._load_itv()
        self._load_diagnosis()

        self.after(800, self._revisar_avisos_itv)

    def _toggle_fullscreen(self):
        current = self.attributes("-fullscreen")
        self.attributes("-fullscreen", not current)

    def _on_resize(self, event):
        w, h = event.width, event.height
        if w < 2 or h < 2:
            return
        fondo = generar_fondo_marca_agua(w, h)
        self.bg_photo = ImageTk.PhotoImage(fondo)
        self.container.configure(image=self.bg_photo)

    def _build_style(self):
        style = ttk.Style(self)
        tema_aplicado = None
        for tema in ("vista", "winnative", "xpnative", "clam", "default"):
            if tema in style.theme_names():
                try:
                    style.theme_use(tema)
                    tema_aplicado = tema
                    break
                except Exception:
                    continue

        style.configure("Body.TFrame", background=BG_LIGHT)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("HeaderTitle.TLabel", background=NAVY, foreground="white", font=("Segoe UI", 22, "bold"))
        style.configure("HeaderSub.TLabel", background=NAVY, foreground="#c9d6f2", font=("Segoe UI", 10))
        style.configure("SectionTitle.TLabel", background=CARD_BG, foreground=TEXT_DARK, font=("Segoe UI", 13, "bold"))
        style.configure("FieldLabel.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=("Segoe UI", 9, "bold"))

        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), background=RED, foreground="white",
                         padding=(16, 10), borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#a5311f"), ("pressed", "#8f2a1b")])

        style.configure("Ghost.TButton", font=("Segoe UI", 10), background=CARD_BG, foreground=TEXT_DARK,
                         padding=(14, 10), borderwidth=1)
        style.map("Ghost.TButton", background=[("active", "#f0f1f3")])

        style.configure("Danger.TButton", font=("Segoe UI", 9, "bold"), background="#fdecea", foreground=RED,
                         padding=(12, 8), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#f8d3ce")])

        style.configure("Treeview", background="white", fieldbackground="white", rowheight=28,
                         font=("Segoe UI", 9), foreground=TEXT_DARK)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#eef1f5",
                         foreground=TEXT_DARK)

        style.configure("TNotebook", background=BG_LIGHT, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(20, 12),
                         background="#dde3ec", foreground=TEXT_DARK)
        style.map("TNotebook.Tab", background=[("selected", CARD_BG)], foreground=[("selected", NAVY)])

    def _build_header(self, parent):
        header = tk.Frame(parent, bg=NAVY, height=90)
        header.pack(fill="x")
        header.pack_propagate(False)
        header_inner = tk.Frame(header, bg=NAVY)
        header_inner.pack(fill="both", expand=True, padx=30, pady=12)

        try:
            self.logo_header_img = ImageTk.PhotoImage(generar_logo_header(70))
            tk.Label(header_inner, image=self.logo_header_img, bg=NAVY).pack(side="left", padx=(0, 15))
        except Exception:
            pass

        title_box = tk.Frame(header_inner, bg=NAVY)
        title_box.pack(side="left", fill="y")
        ttk.Label(title_box, text="TCH", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Gestion de mantenimiento de vehiculos", style="HeaderSub.TLabel").pack(anchor="w")

        today_box = tk.Frame(header_inner, bg=NAVY)
        today_box.pack(side="right")
        ttk.Label(today_box, text=date.today().strftime("%d/%m/%Y"), style="HeaderSub.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(anchor="e")

    def _build_ui(self):
        self._build_header(self.container)
        notebook = ttk.Notebook(self.container)
        notebook.pack(fill="both", expand=True, padx=20, pady=15)

        tab_mant = ttk.Frame(notebook, style="Body.TFrame")
        tab_recambios = ttk.Frame(notebook, style="Body.TFrame")
        tab_itv = ttk.Frame(notebook, style="Body.TFrame")
        tab_diag = ttk.Frame(notebook, style="Body.TFrame")
        tab_ajustes = ttk.Frame(notebook, style="Body.TFrame")

        notebook.add(tab_mant, text="Mantenimiento")
        notebook.add(tab_recambios, text="Recambios")
        notebook.add(tab_itv, text="ITV")
        notebook.add(tab_diag, text="Diagnosis")
        notebook.add(tab_ajustes, text="Ajustes")

        self._build_tab_mantenimiento(tab_mant)
        self._build_tab_recambios(tab_recambios)
        self._build_tab_itv(tab_itv)
        self._build_tab_diagnosis(tab_diag)
        self._build_tab_ajustes(tab_ajustes)

    def _build_tab_mantenimiento(self, parent):
        form_card = Card(parent, padx=25, pady=20)
        form_card.pack(fill="x", padx=15, pady=(15, 12))

        ttk.Label(form_card, text="Nuevo mantenimiento", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 15))

        for i, txt in enumerate(["TIPO DE VEHICULO", "MATRICULA", "FECHA", "KILOMETROS"]):
            ttk.Label(form_card, text=txt, style="FieldLabel.TLabel").grid(row=1, column=i, sticky="w", padx=(0, 10))

        self.tipo_var = tk.StringVar(value=TIPOS[0])
        ttk.Combobox(form_card, textvariable=self.tipo_var, values=TIPOS, state="readonly", width=16).grid(
            row=2, column=0, sticky="we", padx=(0, 10), pady=(4, 15))

        self.matricula_var = tk.StringVar()
        ttk.Entry(form_card, textvariable=self.matricula_var, width=16).grid(
            row=2, column=1, sticky="we", padx=(0, 10), pady=(4, 15))

        self.fecha_var = tk.StringVar(value=date.today().strftime("%d/%m/%Y"))
        ttk.Entry(form_card, textvariable=self.fecha_var, width=16).grid(
            row=2, column=2, sticky="we", padx=(0, 10), pady=(4, 15))

        self.km_var = tk.StringVar()
        ttk.Entry(form_card, textvariable=self.km_var, width=16).grid(row=2, column=3, sticky="we", pady=(4, 15))

        ttk.Label(form_card, text="MANTENIMIENTO A REALIZAR", style="FieldLabel.TLabel").grid(
            row=3, column=0, columnspan=4, sticky="w")
        self.mant_text = tk.Text(form_card, width=80, height=3, font=("Segoe UI", 10),
                                  relief="solid", borderwidth=1, highlightthickness=0)
        self.mant_text.grid(row=4, column=0, columnspan=4, sticky="we", pady=(4, 18))

        btn_frame = tk.Frame(form_card, bg=CARD_BG)
        btn_frame.grid(row=5, column=0, columnspan=4, sticky="w")
        crear_boton(btn_frame, "+ Añadir mantenimiento", self._añadir, "accent").pack(side="left", padx=(0, 10))
        crear_boton(btn_frame, "Limpiar", self._limpiar_formulario, "ghost").pack(side="left")

        for i in range(4):
            form_card.grid_columnconfigure(i, weight=1)

        list_card = Card(parent, padx=25, pady=20)
        list_card.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        list_header = tk.Frame(list_card, bg=CARD_BG)
        list_header.pack(fill="x", pady=(0, 15))
        ttk.Label(list_header, text="Historial de mantenimientos", style="SectionTitle.TLabel").pack(side="left")

        ttk.Label(list_header, text="  (doble clic en una fila para editarla)", style="FieldLabel.TLabel").pack(side="left", padx=(10, 0))

        filtro_frame = tk.Frame(list_header, bg=CARD_BG)
        filtro_frame.pack(side="right")
        ttk.Label(filtro_frame, text="Filtrar:", style="FieldLabel.TLabel").pack(side="left", padx=(0, 8))
        self.filtro_var = tk.StringVar(value="Todos")
        filtro_combo = ttk.Combobox(filtro_frame, textvariable=self.filtro_var, values=["Todos"] + TIPOS,
                                     state="readonly", width=14)
        filtro_combo.pack(side="left", padx=(0, 10))
        filtro_combo.bind("<<ComboboxSelected>>", lambda e: self._load_registros())
        crear_boton(filtro_frame, "Eliminar seleccionado", self._eliminar, "danger").pack(side="left")

        table_frame = tk.Frame(list_card, bg=CARD_BG)
        table_frame.pack(fill="both", expand=True)

        columns = ("id", "tipo", "matricula", "fecha", "km", "mantenimiento")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headers = {"id": "ID", "tipo": "Tipo", "matricula": "Matricula", "fecha": "Fecha",
                   "km": "Kilometros", "mantenimiento": "Mantenimiento"}
        widths = {"id": 40, "tipo": 100, "matricula": 110, "fecha": 100, "km": 100, "mantenimiento": 400}
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.bind("<Double-1>", lambda e: self._editar_mantenimiento())

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _build_tab_itv(self, parent):
        upload_card = Card(parent, padx=25, pady=20)
        upload_card.pack(fill="x", padx=15, pady=(15, 12))

        ttk.Label(upload_card, text="Subir documento ITV (PDF)", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 15))

        ttk.Label(upload_card, text="MATRICULA", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w")
        self.itv_matricula_var = tk.StringVar()
        ttk.Entry(upload_card, textvariable=self.itv_matricula_var, width=20).grid(
            row=2, column=0, sticky="w", padx=(0, 15), pady=(4, 15))

        crear_boton(upload_card, "Seleccionar PDF y analizar", self._subir_itv, "accent").grid(
            row=2, column=1, sticky="w", pady=(4, 15))

        self.itv_estado_lbl = ttk.Label(upload_card, text="", style="FieldLabel.TLabel")
        self.itv_estado_lbl.grid(row=3, column=0, columnspan=3, sticky="w")

        result_card = Card(parent, padx=25, pady=20)
        result_card.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        ttk.Label(result_card, text="Fallos detectados en el documento", style="SectionTitle.TLabel").pack(
            anchor="w", pady=(0, 10))
        self.itv_fallos_text = tk.Text(result_card, height=5, font=("Segoe UI", 10),
                                        relief="solid", borderwidth=1, fg=RED)
        self.itv_fallos_text.pack(fill="x", pady=(0, 15))
        self.itv_fallos_text.configure(state="disabled")

        ttk.Label(result_card, text="Documentos ITV subidos", style="SectionTitle.TLabel").pack(
            anchor="w", pady=(0, 10))

        table_frame = tk.Frame(result_card, bg=CARD_BG)
        table_frame.pack(fill="both", expand=True)

        columns = ("id", "matricula", "fecha", "archivo", "fallos", "vencimiento")
        self.itv_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headers = {"id": "ID", "matricula": "Matricula", "fecha": "Subido", "archivo": "Archivo",
                   "fallos": "Nº fallos", "vencimiento": "Vence ITV"}
        widths = {"id": 40, "matricula": 100, "fecha": 90, "archivo": 260, "fallos": 90, "vencimiento": 110}
        for col in columns:
            self.itv_tree.heading(col, text=headers[col])
            self.itv_tree.column(col, width=widths[col], anchor="w")
        self.itv_tree.pack(fill="both", expand=True, side="left")
        self.itv_tree.bind("<Double-1>", lambda e: self._abrir_seleccionado(self.itv_tree, ITV_DIR, 3))

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.itv_tree.yview)
        self.itv_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _build_tab_diagnosis(self, parent):
        upload_card = Card(parent, padx=25, pady=20)
        upload_card.pack(fill="x", padx=15, pady=(15, 12))

        ttk.Label(upload_card, text="Subir informe de diagnosis (PDF)", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 15))

        ttk.Label(upload_card, text="MATRICULA", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w")
        self.diag_matricula_var = tk.StringVar()
        ttk.Entry(upload_card, textvariable=self.diag_matricula_var, width=20).grid(
            row=2, column=0, sticky="w", padx=(0, 15), pady=(4, 15))

        ttk.Label(upload_card, text="NOTAS", style="FieldLabel.TLabel").grid(row=1, column=1, sticky="w")
        self.diag_notas_var = tk.StringVar()
        ttk.Entry(upload_card, textvariable=self.diag_notas_var, width=40).grid(
            row=2, column=1, sticky="w", padx=(0, 15), pady=(4, 15))

        crear_boton(upload_card, "Seleccionar PDF", self._subir_diagnosis, "accent").grid(
            row=2, column=2, sticky="w", pady=(4, 15))

        list_card = Card(parent, padx=25, pady=20)
        list_card.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        ttk.Label(list_card, text="Informes de diagnosis subidos", style="SectionTitle.TLabel").pack(
            anchor="w", pady=(0, 10))

        table_frame = tk.Frame(list_card, bg=CARD_BG)
        table_frame.pack(fill="both", expand=True)

        columns = ("id", "matricula", "fecha", "archivo", "notas")
        self.diag_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headers = {"id": "ID", "matricula": "Matricula", "fecha": "Fecha", "archivo": "Archivo", "notas": "Notas"}
        widths = {"id": 40, "matricula": 110, "fecha": 100, "archivo": 300, "notas": 250}
        for col in columns:
            self.diag_tree.heading(col, text=headers[col])
            self.diag_tree.column(col, width=widths[col], anchor="w")
        self.diag_tree.pack(fill="both", expand=True, side="left")
        self.diag_tree.bind("<Double-1>", lambda e: self._abrir_seleccionado(self.diag_tree, DIAG_DIR, 3))

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.diag_tree.yview)
        self.diag_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _build_tab_recambios(self, parent):
        form_card = Card(parent, padx=25, pady=20)
        form_card.pack(fill="x", padx=15, pady=(15, 12))

        ttk.Label(form_card, text="Registrar recambio sustituido", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 15))

        for i, txt in enumerate(["TIPO DE VEHICULO", "MATRICULA", "FECHA", "KILOMETROS", "RECAMBIO SUSTITUIDO"]):
            ttk.Label(form_card, text=txt, style="FieldLabel.TLabel").grid(row=1, column=i, sticky="w", padx=(0, 10))

        self.rec_tipo_var = tk.StringVar(value=TIPOS[0])
        ttk.Combobox(form_card, textvariable=self.rec_tipo_var, values=TIPOS, state="readonly", width=14).grid(
            row=2, column=0, sticky="we", padx=(0, 10), pady=(4, 15))

        self.rec_matricula_var = tk.StringVar()
        ttk.Entry(form_card, textvariable=self.rec_matricula_var, width=16).grid(
            row=2, column=1, sticky="we", padx=(0, 10), pady=(4, 15))

        self.rec_fecha_var = tk.StringVar(value=date.today().strftime("%d/%m/%Y"))
        ttk.Entry(form_card, textvariable=self.rec_fecha_var, width=16).grid(
            row=2, column=2, sticky="we", padx=(0, 10), pady=(4, 15))

        self.rec_km_var = tk.StringVar()
        ttk.Entry(form_card, textvariable=self.rec_km_var, width=16).grid(
            row=2, column=3, sticky="we", padx=(0, 10), pady=(4, 15))

        self.rec_nombre_var = tk.StringVar()
        ttk.Entry(form_card, textvariable=self.rec_nombre_var, width=30).grid(
            row=2, column=4, sticky="we", pady=(4, 15))

        ttk.Label(form_card, text="NOTAS (opcional)", style="FieldLabel.TLabel").grid(
            row=3, column=0, columnspan=5, sticky="w")
        self.rec_notas_text = tk.Text(form_card, width=80, height=2, font=("Segoe UI", 10),
                                       relief="solid", borderwidth=1, highlightthickness=0)
        self.rec_notas_text.grid(row=4, column=0, columnspan=5, sticky="we", pady=(4, 18))

        btn_frame = tk.Frame(form_card, bg=CARD_BG)
        btn_frame.grid(row=5, column=0, columnspan=5, sticky="w")
        crear_boton(btn_frame, "+ Añadir recambio", self._añadir_recambio, "accent").pack(side="left", padx=(0, 10))
        crear_boton(btn_frame, "Limpiar", self._limpiar_recambio, "ghost").pack(side="left")

        for i in range(5):
            form_card.grid_columnconfigure(i, weight=1)

        list_card = Card(parent, padx=25, pady=20)
        list_card.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        list_header = tk.Frame(list_card, bg=CARD_BG)
        list_header.pack(fill="x", pady=(0, 15))
        ttk.Label(list_header, text="Historial de recambios", style="SectionTitle.TLabel").pack(side="left")
        crear_boton(list_header, "Eliminar seleccionado", self._eliminar_recambio, "danger").pack(side="right")

        table_frame = tk.Frame(list_card, bg=CARD_BG)
        table_frame.pack(fill="both", expand=True)

        columns = ("id", "matricula", "fecha", "km", "recambio", "notas")
        self.rec_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headers = {"id": "ID", "matricula": "Matricula", "fecha": "Fecha", "km": "Kilometros",
                   "recambio": "Recambio sustituido", "notas": "Notas"}
        widths = {"id": 40, "matricula": 110, "fecha": 100, "km": 100, "recambio": 250, "notas": 250}
        for col in columns:
            self.rec_tree.heading(col, text=headers[col])
            self.rec_tree.column(col, width=widths[col], anchor="w")
        self.rec_tree.pack(fill="both", expand=True, side="left")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.rec_tree.yview)
        self.rec_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _limpiar_recambio(self):
        self.rec_tipo_var.set(TIPOS[0])
        self.rec_matricula_var.set("")
        self.rec_km_var.set("")
        self.rec_fecha_var.set(date.today().strftime("%d/%m/%Y"))
        self.rec_nombre_var.set("")
        self.rec_notas_text.delete("1.0", "end")

    def _añadir_recambio(self):
        tipo = self.rec_tipo_var.get()
        matricula = self.rec_matricula_var.get().strip().upper()
        fecha = self.rec_fecha_var.get().strip()
        km = self.rec_km_var.get().strip()
        recambio = self.rec_nombre_var.get().strip()
        notas = self.rec_notas_text.get("1.0", "end").strip()

        if not matricula:
            messagebox.showwarning("Datos incompletos", "Introduce la matricula del vehiculo.")
            return
        if not recambio:
            messagebox.showwarning("Datos incompletos", "Indica el recambio sustituido.")
            return

        con = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO recambios (matricula, fecha, recambio, kilometros, notas) VALUES (?, ?, ?, ?, ?)",
                (matricula, fecha, recambio, km, notas)
            )
            descripcion = f"Recambio sustituido: {recambio}"
            if notas:
                descripcion += f" ({notas})"
            cur.execute(
                "INSERT INTO mantenimientos (tipo, matricula, fecha, kilometros, mantenimiento) VALUES (?, ?, ?, ?, ?)",
                (tipo, matricula, fecha, int(km) if km.isdigit() else 0, descripcion)
            )
            con.commit()
        except Exception as e:
            messagebox.showerror("Error al guardar", f"No se pudo guardar el recambio:\n{e}")
            return
        finally:
            con.close()

        self._limpiar_recambio()
        self._load_recambios()
        self._load_registros()

    def _load_recambios(self):
        for row in self.rec_tree.get_children():
            self.rec_tree.delete(row)
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        cur.execute("SELECT id, matricula, fecha, kilometros, recambio, notas FROM recambios ORDER BY id DESC")
        for r in cur.fetchall():
            self.rec_tree.insert("", "end", values=r)
        con.close()

    def _eliminar_recambio(self):
        seleccion = self.rec_tree.selection()
        if not seleccion:
            messagebox.showinfo("Eliminar", "Selecciona un registro de la tabla.")
            return
        reg_id = self.rec_tree.item(seleccion[0])["values"][0]
        if messagebox.askyesno("Confirmar", "¿Eliminar el registro seleccionado?"):
            con = sqlite3.connect(DB_PATH, timeout=10)
            cur = con.cursor()
            cur.execute("DELETE FROM recambios WHERE id = ?", (reg_id,))
            con.commit()
            con.close()
            self._load_recambios()

    def _build_tab_ajustes(self, parent):
        email_card = Card(parent, padx=25, pady=20)
        email_card.pack(fill="x", padx=15, pady=(15, 12))
        ttk.Label(email_card, text="Configuracion de correo (avisos de ITV)", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 15))

        campos_email = [
            ("Servidor SMTP", "smtp_server"), ("Puerto SMTP", "smtp_port"),
            ("Correo remitente", "smtp_user"), ("Contraseña / clave de aplicacion", "smtp_password"),
            ("Correo destino de avisos", "email_destino"),
        ]
        self.email_vars = {}
        for i, (label, key) in enumerate(campos_email):
            ttk.Label(email_card, text=label.upper(), style="FieldLabel.TLabel").grid(
                row=1 + i * 2, column=0, sticky="w", pady=(8, 0))
            var = tk.StringVar(value=self.config_data.get(key, ""))
            show = "*" if "password" in key else ""
            ttk.Entry(email_card, textvariable=var, width=50, show=show).grid(
                row=2 + i * 2, column=0, sticky="w", pady=(2, 4))
            self.email_vars[key] = var

        github_card = Card(parent, padx=25, pady=20)
        github_card.pack(fill="x", padx=15, pady=(0, 12))
        ttk.Label(github_card, text="Sincronizacion con GitHub (para app iOS)", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 15))

        campos_github = [
            ("Repositorio (usuario/repositorio)", "github_repo"),
            ("Token de acceso personal", "github_token"),
            ("Rama", "github_branch"),
            ("Ruta del archivo de datos", "github_path"),
        ]
        self.github_vars = {}
        for i, (label, key) in enumerate(campos_github):
            ttk.Label(github_card, text=label.upper(), style="FieldLabel.TLabel").grid(
                row=1 + i * 2, column=0, sticky="w", pady=(8, 0))
            var = tk.StringVar(value=self.config_data.get(key, ""))
            show = "*" if "token" in key else ""
            ttk.Entry(github_card, textvariable=var, width=50, show=show).grid(
                row=2 + i * 2, column=0, sticky="w", pady=(2, 4))
            self.github_vars[key] = var

        btn_frame = tk.Frame(parent, bg=BG_LIGHT)
        btn_frame.pack(fill="x", padx=15, pady=(0, 15))
        crear_boton(btn_frame, "Guardar ajustes", self._guardar_ajustes, "accent").pack(side="left", padx=(0, 10))
        crear_boton(btn_frame, "Sincronizar ahora con GitHub", self._sincronizar_ahora, "ghost").pack(side="left")

        self.ajustes_estado_lbl = ttk.Label(parent, text="", style="FieldLabel.TLabel", background=BG_LIGHT)
        self.ajustes_estado_lbl.pack(anchor="w", padx=20)

    def _guardar_ajustes(self):
        for key, var in self.email_vars.items():
            self.config_data[key] = var.get().strip()
        for key, var in self.github_vars.items():
            self.config_data[key] = var.get().strip()
        guardar_config(self.config_data)
        self.ajustes_estado_lbl.configure(text="Ajustes guardados correctamente.")

    def _sincronizar_ahora(self):
        self._guardar_ajustes()
        self.ajustes_estado_lbl.configure(text="Sincronizando con GitHub...")

        def tarea():
            ok, msg = sincronizar_github(self.config_data)
            self.after(0, lambda: self.ajustes_estado_lbl.configure(text=msg))

        threading.Thread(target=tarea, daemon=True).start()

    def _abrir_seleccionado(self, tree, carpeta, col_index):
        seleccion = tree.selection()
        if not seleccion:
            return
        archivo = tree.item(seleccion[0])["values"][col_index]
        ruta = os.path.join(carpeta, archivo)
        if os.path.exists(ruta):
            try:
                os.startfile(ruta)
            except Exception:
                messagebox.showinfo("Archivo", ruta)

    def _limpiar_formulario(self):
        self.matricula_var.set("")
        self.km_var.set("")
        self.fecha_var.set(date.today().strftime("%d/%m/%Y"))
        self.mant_text.delete("1.0", "end")

    def _añadir(self):
        tipo = self.tipo_var.get()
        matricula = self.matricula_var.get().strip().upper()
        fecha = self.fecha_var.get().strip()
        km = self.km_var.get().strip()
        mantenimiento = self.mant_text.get("1.0", "end").strip()

        if not matricula:
            messagebox.showwarning("Datos incompletos", "Introduce la matricula del vehiculo.")
            return
        if not km.isdigit():
            messagebox.showwarning("Datos incompletos", "Los kilometros deben ser un numero.")
            return
        if not mantenimiento:
            messagebox.showwarning("Datos incompletos", "Describe el mantenimiento a realizar.")
            return

        con = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO mantenimientos (tipo, matricula, fecha, kilometros, mantenimiento) VALUES (?, ?, ?, ?, ?)",
                (tipo, matricula, fecha, int(km), mantenimiento)
            )
            con.commit()
        except Exception as e:
            messagebox.showerror("Error al guardar", f"No se pudo guardar el mantenimiento:\n{e}")
            return
        finally:
            con.close()
        self._limpiar_formulario()
        self._load_registros()

    def _load_registros(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        filtro = self.filtro_var.get()
        if filtro == "Todos":
            cur.execute("SELECT id, tipo, matricula, fecha, kilometros, mantenimiento FROM mantenimientos ORDER BY id DESC")
        else:
            cur.execute("SELECT id, tipo, matricula, fecha, kilometros, mantenimiento FROM mantenimientos WHERE tipo = ? ORDER BY id DESC", (filtro,))
        for r in cur.fetchall():
            self.tree.insert("", "end", values=r)
        con.close()
        self.title(f"TCH - Mantenimiento de Vehiculos ({len(self.tree.get_children())} registros cargados)")
        self.tree.update_idletasks()

    def _editar_mantenimiento(self):
        seleccion = self.tree.selection()
        if not seleccion:
            return
        valores = self.tree.item(seleccion[0])["values"]
        reg_id, tipo, matricula, fecha, km, mantenimiento = valores

        ventana = tk.Toplevel(self)
        ventana.title("Editar mantenimiento")
        ventana.configure(bg=CARD_BG)
        ventana.transient(self)
        ventana.grab_set()
        ventana.resizable(False, False)

        pad = tk.Frame(ventana, bg=CARD_BG, padx=25, pady=25)
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, text="Editar mantenimiento", style="SectionTitle.TLabel").pack(anchor="w", pady=(0, 15))

        ttk.Label(pad, text="TIPO DE VEHICULO", style="FieldLabel.TLabel").pack(anchor="w")
        tipo_var = tk.StringVar(value=tipo)
        ttk.Combobox(pad, textvariable=tipo_var, values=TIPOS, state="readonly", width=20).pack(
            anchor="w", pady=(2, 12))

        ttk.Label(pad, text="MATRICULA", style="FieldLabel.TLabel").pack(anchor="w")
        matricula_var = tk.StringVar(value=matricula)
        ttk.Entry(pad, textvariable=matricula_var, width=24).pack(anchor="w", pady=(2, 12))

        ttk.Label(pad, text="FECHA", style="FieldLabel.TLabel").pack(anchor="w")
        fecha_var = tk.StringVar(value=fecha)
        ttk.Entry(pad, textvariable=fecha_var, width=24).pack(anchor="w", pady=(2, 12))

        ttk.Label(pad, text="KILOMETROS", style="FieldLabel.TLabel").pack(anchor="w")
        km_var = tk.StringVar(value=km)
        ttk.Entry(pad, textvariable=km_var, width=24).pack(anchor="w", pady=(2, 12))

        ttk.Label(pad, text="MANTENIMIENTO", style="FieldLabel.TLabel").pack(anchor="w")
        mant_edit_text = tk.Text(pad, width=50, height=4, font=("Segoe UI", 10),
                                  relief="solid", borderwidth=1, highlightthickness=0)
        mant_edit_text.insert("1.0", mantenimiento)
        mant_edit_text.pack(anchor="w", pady=(2, 15))

        def guardar_cambios():
            nuevo_tipo = tipo_var.get()
            nueva_matricula = matricula_var.get().strip().upper()
            nueva_fecha = fecha_var.get().strip()
            nuevo_km = km_var.get().strip()
            nuevo_mant = mant_edit_text.get("1.0", "end").strip()

            if not nueva_matricula or not nuevo_km.isdigit() or not nuevo_mant:
                messagebox.showwarning("Datos incompletos", "Revisa la matricula, los kilometros y el mantenimiento.")
                return

            con = sqlite3.connect(DB_PATH, timeout=10)
            try:
                cur = con.cursor()
                cur.execute(
                    "UPDATE mantenimientos SET tipo=?, matricula=?, fecha=?, kilometros=?, mantenimiento=? WHERE id=?",
                    (nuevo_tipo, nueva_matricula, nueva_fecha, int(nuevo_km), nuevo_mant, reg_id)
                )
                con.commit()
            except Exception as e:
                messagebox.showerror("Error al guardar", f"No se pudo actualizar el registro:\n{e}")
                return
            finally:
                con.close()

            self._load_registros()
            ventana.destroy()

        def eliminar_desde_ventana():
            if messagebox.askyesno("Confirmar", "¿Eliminar este registro?"):
                con = sqlite3.connect(DB_PATH, timeout=10)
                cur = con.cursor()
                cur.execute("DELETE FROM mantenimientos WHERE id = ?", (reg_id,))
                con.commit()
                con.close()
                self._load_registros()
                ventana.destroy()

        btn_frame = tk.Frame(pad, bg=CARD_BG)
        btn_frame.pack(anchor="w")
        crear_boton(btn_frame, "Guardar cambios", guardar_cambios, "accent").pack(side="left", padx=(0, 10))
        crear_boton(btn_frame, "Eliminar", eliminar_desde_ventana, "danger").pack(side="left", padx=(0, 10))
        crear_boton(btn_frame, "Cancelar", ventana.destroy, "ghost").pack(side="left")

        ventana.update_idletasks()
        ancho = pad.winfo_reqwidth() + 20
        alto = pad.winfo_reqheight() + 20
        x = self.winfo_x() + (self.winfo_width() - ancho) // 2
        y = self.winfo_y() + (self.winfo_height() - alto) // 2
        ventana.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _eliminar(self):
        seleccion = self.tree.selection()
        if not seleccion:
            messagebox.showinfo("Eliminar", "Selecciona un registro de la tabla.")
            return
        reg_id = self.tree.item(seleccion[0])["values"][0]
        if messagebox.askyesno("Confirmar", "¿Eliminar el registro seleccionado?"):
            con = sqlite3.connect(DB_PATH, timeout=10)
            cur = con.cursor()
            cur.execute("DELETE FROM mantenimientos WHERE id = ?", (reg_id,))
            con.commit()
            con.close()
            self._load_registros()

    def _subir_itv(self):
        matricula = self.itv_matricula_var.get().strip().upper()
        if not matricula:
            messagebox.showwarning("Datos incompletos", "Introduce la matricula antes de subir el documento.")
            return

        ruta_origen = filedialog.askopenfilename(title="Selecciona el documento ITV", filetypes=[("PDF", "*.pdf")])
        if not ruta_origen:
            return

        nombre_destino = f"{matricula}_{date.today().strftime('%Y%m%d')}_{os.path.basename(ruta_origen)}"
        ruta_destino = os.path.join(ITV_DIR, nombre_destino)
        shutil.copy2(ruta_origen, ruta_destino)

        texto = extraer_texto_pdf(ruta_destino)
        fallos = detectar_fallos(texto)
        vencimiento = detectar_vencimiento(texto)

        self.itv_fallos_text.configure(state="normal")
        self.itv_fallos_text.delete("1.0", "end")
        if fallos:
            for f in fallos:
                self.itv_fallos_text.insert("end", f"⚠ {f}\n")
        else:
            self.itv_fallos_text.insert("end", "No se han detectado fallos en el texto del documento.")
        self.itv_fallos_text.configure(state="disabled")

        con = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO itv_documentos (matricula, fecha, archivo, fallos, vencimiento, avisado) VALUES (?, ?, ?, ?, ?, 0)",
                (matricula, date.today().strftime("%d/%m/%Y"), nombre_destino, "\n".join(fallos), vencimiento)
            )
            con.commit()
        except Exception as e:
            messagebox.showerror("Error al guardar", f"No se pudo guardar el documento ITV:\n{e}")
            return
        finally:
            con.close()

        texto_venc = f" Proxima ITV detectada: {vencimiento}." if vencimiento else " No se detecto fecha de vencimiento."
        self.itv_estado_lbl.configure(
            text=f"Documento analizado: {len(fallos)} posible(s) fallo(s) detectado(s).{texto_venc}")
        self.itv_matricula_var.set("")
        self._load_itv()

    def _load_itv(self):
        for row in self.itv_tree.get_children():
            self.itv_tree.delete(row)
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        cur.execute("SELECT id, matricula, fecha, archivo, fallos, vencimiento FROM itv_documentos ORDER BY id DESC")
        for r in cur.fetchall():
            num_fallos = len([l for l in (r[4] or "").split("\n") if l.strip()])
            self.itv_tree.insert("", "end", values=(r[0], r[1], r[2], r[3], num_fallos, r[5] or ""))
        con.close()

    def _revisar_avisos_itv(self):
        hoy = date.today().strftime("%d/%m/%Y")
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        cur.execute(
            "SELECT id, matricula, vencimiento FROM itv_documentos WHERE vencimiento = ? AND avisado = 0", (hoy,))
        pendientes = cur.fetchall()
        con.close()

        for reg_id, matricula, vencimiento in pendientes:
            mensaje = f"La ITV del vehiculo con matricula {matricula} vence hoy ({vencimiento})."
            messagebox.showwarning("Aviso ITV", mensaje)

            def enviar(reg_id=reg_id, matricula=matricula, mensaje=mensaje):
                ok, _ = enviar_email(self.config_data, f"Aviso ITV - {matricula}", mensaje)
                if ok:
                    con2 = sqlite3.connect(DB_PATH, timeout=10)
                    cur2 = con2.cursor()
                    cur2.execute("UPDATE itv_documentos SET avisado = 1 WHERE id = ?", (reg_id,))
                    con2.commit()
                    con2.close()

            threading.Thread(target=enviar, daemon=True).start()

    def _subir_diagnosis(self):
        matricula = self.diag_matricula_var.get().strip().upper()
        if not matricula:
            messagebox.showwarning("Datos incompletos", "Introduce la matricula antes de subir el documento.")
            return

        ruta_origen = filedialog.askopenfilename(title="Selecciona el informe de diagnosis", filetypes=[("PDF", "*.pdf")])
        if not ruta_origen:
            return

        nombre_destino = f"{matricula}_{date.today().strftime('%Y%m%d')}_{os.path.basename(ruta_origen)}"
        ruta_destino = os.path.join(DIAG_DIR, nombre_destino)
        shutil.copy2(ruta_origen, ruta_destino)

        con = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO diagnosis_documentos (matricula, fecha, archivo, notas) VALUES (?, ?, ?, ?)",
                (matricula, date.today().strftime("%d/%m/%Y"), nombre_destino, self.diag_notas_var.get().strip())
            )
            con.commit()
        except Exception as e:
            messagebox.showerror("Error al guardar", f"No se pudo guardar el informe de diagnosis:\n{e}")
            return
        finally:
            con.close()

        self.diag_matricula_var.set("")
        self.diag_notas_var.set("")
        self._load_diagnosis()

    def _load_diagnosis(self):
        for row in self.diag_tree.get_children():
            self.diag_tree.delete(row)
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        cur.execute("SELECT id, matricula, fecha, archivo, notas FROM diagnosis_documentos ORDER BY id DESC")
        for r in cur.fetchall():
            self.diag_tree.insert("", "end", values=r)
        con.close()


if __name__ == "__main__":
    init_db()
    app = TCHApp()
    app.mainloop()