# -*- coding: utf-8 -*-
"""
Truck Rental - Interfaz web que conecta Oracle (capa transaccional)
con MongoDB Atlas (capa analitica).

Ejecutar:   python -m streamlit run app.py

Flujo:
  * Los arriendos y las multas se registran en Oracle (fuente de verdad).
  * Cada cambio se refleja de inmediato en MongoDB (coleccion arriendos_historicos).
  * El dashboard se calcula en MongoDB con pipelines de agregacion.

Valores que aparecen ya escritos en el formulario de conexion (no son secretos):
  si creas un archivo config_local.json junto a este archivo, esos valores reemplazan
  a los de POR_DEFECTO. Las contrasenas nunca se guardan en ningun archivo.
"""
import base64
import datetime as dt
import html
import json
import re
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

import oracledb
import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import PyMongoError

COLECCION = "arriendos_historicos"
GARANTIA_POR_DEFECTO = 100000  # regla de la Evaluacion 2: NVL(garantia, 100000)
TIPOS_MULTA = ["RETRASO", "DAÑO", "GRUA"]

POR_DEFECTO = {
    "o_host": "localhost", "o_port": "1521", "o_serv": "XEPDB1", "o_user": "alumno",
    "m_host": "cluster0.xxxxx.mongodb.net", "m_user": "", "m_db": "truck_rental_db",
}


def cargar_por_defecto():
    valores = dict(POR_DEFECTO)
    try:
        valores.update(json.loads(Path(__file__).with_name("config_local.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return valores


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def clp(n):
    """Formatea un numero como pesos chilenos: $1.234.567"""
    return "$" + f"{int(round(n or 0)):,}".replace(",", ".")


def fecha(d):
    return d.strftime("%d-%m-%Y") if d else "—"


def nombre_cliente(r):
    partes = [r.get("pnombre_cli"), r.get("snombre_cli"), r.get("appaterno_cli")]
    return " ".join(p for p in partes if p).title()


def error_oracle(e):
    txt = str(e)
    if "ORA-20001" in txt:
        msg = txt.split("ORA-20001:", 1)[1].split("\n")[0].strip()
        return f"El trigger de Oracle rechazó el registro: {msg}"
    if "ORA-01017" in txt:
        return "Usuario o contraseña de Oracle incorrectos."
    if "ORA-02291" in txt:
        return "El camión, el cliente o el arriendo elegido no existe en Oracle."
    if "ORA-00942" in txt:
        return "Falta una tabla en Oracle (¿ya creaste la tabla MULTA?). Detalle: " + txt
    if "DPY-6005" in txt or "ORA-12541" in txt or "DPY-6001" in txt:
        return ("No se pudo conectar con Oracle. Revisa que el servicio de Oracle XE "
                "esté encendido y que host, puerto y servicio sean correctos.")
    return txt


def error_mongo(e):
    txt = str(e)
    low = txt.lower()
    if "authentication failed" in low or "bad auth" in low:
        return "Usuario o contraseña de MongoDB incorrectos."
    if "ssl" in low or "timed out" in low or "timeout" in low or "no replica set members" in low:
        return ("MongoDB Atlas no respondió. Revisa tu internet y que tu IP esté "
                "autorizada en Network Access.")
    return txt


# ----------------------------------------------------------------------------
# Conexiones
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _cliente_mongo(uri):
    return MongoClient(uri, serverSelectionTimeoutMS=8000)


def mongo_uri(usuario, clave, host):
    return (f"mongodb+srv://{quote_plus(usuario)}:{quote_plus(clave)}@{host}/"
            "?retryWrites=true&w=majority&appName=TruckRental")


def coleccion():
    cfg = st.session_state["cfg"]
    return _cliente_mongo(cfg["m_uri"])[cfg["m_db"]][COLECCION]


@contextmanager
def oracle():
    c = st.session_state["cfg"]
    conn = oracledb.connect(
        user=c["o_user"], password=c["o_pass"],
        dsn=f'{c["o_host"]}:{c["o_port"]}/{c["o_serv"]}',
    )
    try:
        yield conn
    finally:
        conn.close()


def fetch(conn, sql, binds=None):
    """Ejecuta un SELECT y devuelve una lista de dicts con claves en minuscula."""
    cur = conn.cursor()
    cur.execute(sql, binds or {})
    cols = [d[0].lower() for d in cur.description]
    filas = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    return filas


def probar_conexiones(o_host, o_port, o_serv, o_user, o_pass, m_uri):
    errores = []
    try:
        conn = oracledb.connect(user=o_user, password=o_pass, dsn=f"{o_host}:{o_port}/{o_serv}")
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM dual")
        cur.fetchone()
        conn.close()
    except oracledb.Error as e:
        errores.append("Oracle: " + error_oracle(e))
    try:
        _cliente_mongo(m_uri).admin.command("ping")
    except PyMongoError as e:
        errores.append("MongoDB: " + error_mongo(e))
    return errores


# ----------------------------------------------------------------------------
# Oracle -> documento de MongoDB
# ----------------------------------------------------------------------------
SQL_ARRIENDOS = """
SELECT a.id_arriendo, a.nro_patente, a.numrun_cli, a.fecha_ini_arriendo,
       a.dias_solicitados, a.fecha_devolucion,
       c.valor_arriendo_dia, c.valor_garantia_dia,
       m.nombre_marca, t.nombre_tipo_camion,
       cl.pnombre_cli, cl.snombre_cli, cl.appaterno_cli,
       tc.nombre_tipo_cli
FROM arriendo_camion a
JOIN camion c        ON c.nro_patente = a.nro_patente
JOIN marca m         ON m.id_marca = c.id_marca
JOIN tipo_camion t   ON t.id_tipo_camion = c.id_tipo_camion
JOIN cliente cl      ON cl.numrun_cli = a.numrun_cli
JOIN tipo_cliente tc ON tc.id_tipo_cli = cl.id_tipo_cli
"""

SQL_MULTAS = """
SELECT id_multa, id_arriendo, tipo, monto, estado, dias_retraso, descripcion
FROM multa
"""

SQL_CAMIONES = """
SELECT c.nro_patente, c.valor_arriendo_dia, c.valor_garantia_dia,
       m.nombre_marca, t.nombre_tipo_camion
FROM camion c
JOIN marca m       ON m.id_marca = c.id_marca
JOIN tipo_camion t ON t.id_tipo_camion = c.id_tipo_camion
ORDER BY c.nro_patente
"""

SQL_CLIENTES = """
SELECT numrun_cli, dvrun_cli, pnombre_cli, snombre_cli, appaterno_cli
FROM cliente
ORDER BY appaterno_cli, pnombre_cli
"""


def fetch_arriendos(conn, id_arriendo=None):
    sql = SQL_ARRIENDOS
    binds = {}
    if id_arriendo is not None:
        sql += " WHERE a.id_arriendo = :p_id"
        binds["p_id"] = int(id_arriendo)
    return fetch(conn, sql + " ORDER BY a.id_arriendo", binds)


def fetch_multas(conn, id_arriendo=None):
    sql = SQL_MULTAS
    binds = {}
    if id_arriendo is not None:
        sql += " WHERE id_arriendo = :p_id"
        binds["p_id"] = int(id_arriendo)
    return fetch(conn, sql + " ORDER BY id_multa", binds)


def multa_doc(m):
    """Una multa de Oracle como subdocumento. Cada tipo guarda solo sus campos."""
    d = {
        "id_multa": int(m["id_multa"]),
        "tipo": m["tipo"],
        "monto": int(m["monto"]),
        "estado": m["estado"],
    }
    detalles = {}
    if m.get("dias_retraso") is not None:
        detalles["dias_retraso"] = int(m["dias_retraso"])
    if m.get("descripcion"):
        detalles["descripcion"] = m["descripcion"]
    if detalles:
        d["detalles"] = detalles
    return d


def build_doc(a, multas):
    """Arma el documento de arriendos_historicos a partir de las filas de Oracle."""
    ini = a["fecha_ini_arriendo"]
    dias = int(a["dias_solicitados"] or 0)
    valor_dia = int(a["valor_arriendo_dia"])
    garantia_real = a["valor_garantia_dia"] is not None
    id_arr = int(a["id_arriendo"])
    return {
        "_id": f"ARR-{ini.year}-{id_arr:04d}",
        "id_oracle": id_arr,
        "anno_proceso": int(ini.year),
        "camion": {
            "patente": a["nro_patente"],
            "marca": a["nombre_marca"],
            "tipo_camion": a["nombre_tipo_camion"],
        },
        "cliente": {
            "rut": str(int(a["numrun_cli"])),
            "nombre": nombre_cliente(a),
            "tipo_cliente": a["nombre_tipo_cli"],
        },
        "fechas": {
            "inicio": ini,
            "devolucion": a["fecha_devolucion"],
            "dias_solicitados": dias,
        },
        "finanzas": {
            "valor_arriendo_dia": valor_dia,
            "total_arriendo": valor_dia * dias,
            "garantia_aplicada": "REAL" if garantia_real else "POR DEFECTO",
            "monto_garantia": int(a["valor_garantia_dia"]) if garantia_real else GARANTIA_POR_DEFECTO,
        },
        "multas": [multa_doc(m) for m in multas],
        "origen": "oracle",
        "sincronizado": dt.datetime.now(dt.timezone.utc),
    }


def sincronizar_uno(conn, col, id_arriendo):
    """Vuelve a leer un arriendo desde Oracle y deja su documento al dia en Mongo."""
    filas = fetch_arriendos(conn, id_arriendo)
    if not filas:
        col.delete_many({"id_oracle": int(id_arriendo)})
        return
    doc = build_doc(filas[0], fetch_multas(conn, id_arriendo))
    col.replace_one({"_id": doc["_id"]}, doc, upsert=True)


def sincronizar_todo(conn, col, borrar_pruebas=False):
    """Copia TODO Oracle a Mongo (equivale al JOIN de 5 tablas, hecho una sola vez)."""
    arriendos = fetch_arriendos(conn)
    por_arriendo = defaultdict(list)
    for m in fetch_multas(conn):
        por_arriendo[int(m["id_arriendo"])].append(m)
    docs = [build_doc(a, por_arriendo.get(int(a["id_arriendo"]), [])) for a in arriendos]
    nuevos = actualizados = 0
    if docs:
        res = col.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs])
        nuevos, actualizados = res.upserted_count, res.modified_count
    ids = [d["_id"] for d in docs]
    huerfanos = col.delete_many({"origen": "oracle", "_id": {"$nin": ids}}).deleted_count
    pruebas = 0
    if borrar_pruebas:
        pruebas = col.delete_many({"origen": {"$ne": "oracle"}}).deleted_count
    return {"total": len(docs), "nuevos": nuevos, "actualizados": actualizados,
            "huerfanos": huerfanos, "pruebas": pruebas}


def guardar_en_mongo(accion):
    """Ejecuta la escritura en Mongo; si falla, avisa sin perder lo ya guardado en Oracle."""
    try:
        accion()
        return True
    except PyMongoError as e:
        st.warning("Se guardó en Oracle, pero MongoDB no respondió "
                   f"({error_mongo(e)}). Usa **Sincronización → Sincronizar todo** para "
                   "dejar MongoDB al día.")
        return False


# ----------------------------------------------------------------------------
# Pipelines de agregacion (Dashboard)
# ----------------------------------------------------------------------------
PIPE_GARANTIAS = [
    {"$group": {"_id": "$finanzas.garantia_aplicada", "cantidad": {"$sum": 1}}},
    {"$project": {"_id": 0, "Tipo_Garantia": "$_id", "Cantidad_Arriendos": "$cantidad"}},
]
PIPE_RENTABILIDAD = [
    {"$addFields": {"total_multas": {"$sum": "$multas.monto"}}},
    {"$addFields": {"ingreso_total": {"$add": ["$finanzas.total_arriendo", "$total_multas"]}}},
    {"$group": {"_id": "$camion.patente", "Ingreso_Total": {"$sum": "$ingreso_total"}}},
    {"$sort": {"Ingreso_Total": -1}},
]
PIPE_TIPO_CLIENTE = [
    {"$group": {"_id": "$cliente.tipo_cliente", "Ingresos_Generados": {"$sum": "$finanzas.total_arriendo"}}},
    {"$sort": {"Ingresos_Generados": -1}},
]
PIPE_MULTAS_PEND = [
    {"$unwind": "$multas"},
    {"$match": {"multas.estado": "PENDIENTE"}},
    {"$group": {"_id": None, "Total_Por_Cobrar": {"$sum": "$multas.monto"}, "Cantidad_Multas": {"$sum": 1}}},
    {"$project": {"_id": 0, "Total_Por_Cobrar_CLP": "$Total_Por_Cobrar",
                  "Cantidad_Multas_Pendientes": "$Cantidad_Multas"}},
]
PIPE_DIAS_MARCA = [
    {"$group": {"_id": "$camion.marca", "PromedioDiasArriendo": {"$avg": "$fechas.dias_solicitados"}}},
    {"$sort": {"PromedioDiasArriendo": -1}},
]
PIPE_FACET = [
    {"$facet": {
        "Garantias": [
            {"$group": {"_id": "$finanzas.garantia_aplicada", "total": {"$sum": 1}}},
        ],
        "IngresosPorTipoCliente": [
            {"$group": {"_id": "$cliente.tipo_cliente", "ingresos": {"$sum": "$finanzas.total_arriendo"}}},
        ],
        "MultasPendientes": [
            {"$unwind": "$multas"},
            {"$match": {"multas.estado": "PENDIENTE"}},
            {"$group": {"_id": None, "monto_total": {"$sum": "$multas.monto"}, "cantidad": {"$sum": 1}}},
        ],
    }},
]
PIPE_DETALLE_PEND = [
    {"$unwind": "$multas"},
    {"$match": {"multas.estado": "PENDIENTE"}},
    {"$project": {"_id": 1, "cliente": "$cliente.nombre", "patente": "$camion.patente",
                  "id_multa": "$multas.id_multa", "tipo": "$multas.tipo", "monto": "$multas.monto"}},
    {"$sort": {"id_multa": 1}},
]
PIPE_CLIENTES_PEND = [
    {"$unwind": "$multas"},
    {"$match": {"multas.estado": "PENDIENTE"}},
    {"$group": {"_id": "$cliente.rut", "nombre": {"$first": "$cliente.nombre"},
                "total": {"$sum": "$multas.monto"}, "cantidad": {"$sum": 1}}},
    {"$sort": {"nombre": 1}},
]
PIPE_LISTA_CLIENTES = [
    {"$group": {"_id": "$cliente.rut", "nombre": {"$first": "$cliente.nombre"},
                "tipo": {"$first": "$cliente.tipo_cliente"}, "arriendos": {"$sum": 1}}},
    {"$sort": {"nombre": 1}},
]
PIPE_TOTAL_MULTAS = [
    {"$group": {"_id": None, "n": {"$sum": {"$size": {"$ifNull": ["$multas", []]}}}}},
]


def mostrar_pipeline(titulo, pipeline):
    with st.expander(f"Ver la consulta de MongoDB · {titulo}"):
        st.code(json.dumps(pipeline, indent=2, ensure_ascii=False), language="json")


def a_df(cursor):
    return pd.DataFrame(list(cursor))


# ----------------------------------------------------------------------------
# Apariencia: paleta, iconos propios (SVG) y estilos
# ----------------------------------------------------------------------------
FONDO = "#000000"
LATERAL = "#0A0A0A"
TARJETA = "#161616"
CAMPO = "#232323"
BORDE = "rgba(255,255,255,0.11)"
TEXTO = "#F5F5F5"
APAGADO = "#9A9A9A"
ACENTO = "#4DA3FF"
LILA = "#7C5CE0"
TINTA = "#0B0F19"
PALETA = ["#4DA3FF", "#FFD23F", "#35B8A5", "#B04CC9", "#F0508A", "#41BCD6", "#7C5CE0", "#FF6B57"]
TONOS = {"naranja": "#FFD23F", "verde": "#35B8A5", "lila": "#B76CE0", "celeste": "#4DA3FF", "gris": "#9A9A9A"}
KPI_COLORES = [("#3D8BF2", "#FFFFFF"), ("#8E3FB0", "#FFFFFF"), ("#2E9688", "#FFFFFF"), ("#FFD23F", "#1A1400")]
ESTADO_TONO = {"PENDIENTE": "naranja", "PAGADA": "verde", "CONDONADA": "lila"}

# Iconos de Truck Rental: trazo simple de 24x24, dibujados para esta app.
ICONOS = {
    "camion": ('<path d="M2 6.6A1.6 1.6 0 0 1 3.6 5H13a1.6 1.6 0 0 1 1.6 1.6V16H2z"/>'
               '<path d="M14.6 9h3.1l3.3 3.6V16h-6.4"/>'
               '<circle cx="7" cy="17.6" r="2"/><circle cx="17.4" cy="17.6" r="2"/>'),
    "panel": ('<rect x="3.5" y="12" width="4" height="8" rx="1.2"/>'
              '<rect x="10" y="4" width="4" height="16" rx="1.2"/>'
              '<rect x="16.5" y="9" width="4" height="11" rx="1.2"/>'),
    "alerta": '<path d="M12 3.6 21.4 19.6H2.6z"/><path d="M12 10v4.4"/><path d="M12 17.2v.1"/>',
    "lista": '<path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/><path d="M4.2 6.5h.1M4.2 12h.1M4.2 17.5h.1"/>',
    "sync": ('<path d="M20 11a8 8 0 0 0-14.3-4.6L4 8.4"/><path d="M4 4v4.4h4.4"/>'
             '<path d="M4 13a8 8 0 0 0 14.3 4.6l1.7-2"/><path d="M20 20v-4.4h-4.4"/>'),
    "base": ('<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/>'
             '<path d="M4.5 12v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/>'),
    "moneda": ('<circle cx="12" cy="12" r="8.5"/><path d="M12 7v10"/>'
               '<path d="M14.6 9.4c-.4-1-1.4-1.5-2.6-1.5-1.5 0-2.6.7-2.6 1.8 0 2.5 5.2 1.3 5.2 3.8 0 1.1-1.1 1.8-2.6 1.8-1.3 0-2.3-.6-2.7-1.6"/>'),
    "reloj": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.2V12l3.2 2"/>',
    "documento": ('<path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4"/><path d="M9 12.5h6M9 16h6"/>'),
    "enchufe": ('<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0z"/><path d="M12 17v4"/>'),
    "usuario": '<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20.2c.9-3.9 3.7-6.1 7.5-6.1s6.6 2.2 7.5 6.1"/>',
    "salir": '<path d="M10 4H5.5A1.5 1.5 0 0 0 4 5.5v13A1.5 1.5 0 0 0 5.5 20H10"/><path d="M15 8l4 4-4 4M19 12H9"/>',
}


def svg(nombre, tam=20, color="currentColor", ancho=1.8):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{tam}" height="{tam}" viewBox="0 0 24 24" '
            f'fill="none" stroke="{color}" stroke-width="{ancho}" stroke-linecap="round" '
            f'stroke-linejoin="round">{ICONOS[nombre]}</svg>')


def svg_uri(nombre):
    """El icono como data URI, para usarlo de mascara CSS (toma el color del texto)."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg(nombre, 24, "#000").encode()).decode()


def logo(tam=40):
    return (f'<span class="tr-logo" style="width:{tam}px;height:{tam}px;border-radius:{tam * 0.3:.0f}px">'
            f'{svg("camion", int(tam * 0.6), "#FFFFFF", 1.9)}</span>')


CSS_BASE = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, .stApp, .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp li, .stApp td, .stApp th,
.stApp button, .stApp input, .stApp textarea, .stApp [data-baseweb] {
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
}
.stApp { background: __FONDO__; color: __TEXTO__; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stAppDeployButton"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stHeaderActionElements"] { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container {
  padding: 2.4rem 3rem 4rem; max-width: 1480px;
}
h1, h2, h3 { color: __TEXTO__; letter-spacing: -0.01em; }
h1 { font-size: 1.7rem !important; font-weight: 600 !important; padding: 0 0 .2rem !important; margin: 0; }
[data-testid="stCaptionContainer"], .stApp small { color: __APAGADO__; }
hr { border-color: __BORDE__; }

/* migas de pan + titulo */
.tr-migas { color: __APAGADO__; font-size: .85rem; margin-bottom: .35rem; }
.tr-migas b { color: __TEXTO__; font-weight: 500; }
.tr-migas span { margin: 0 .4rem; opacity: .5; }

/* barra lateral */
[data-testid="stSidebar"] { background: __LATERAL__; border-right: 1px solid __BORDE__; }
[data-testid="stSidebar"] [data-testid="stSidebarContent"] { padding-top: .6rem; }
.tr-marca { display: flex; align-items: center; gap: .75rem; padding: .9rem .4rem 1.3rem; }
.tr-marca b { font-size: 1.12rem; font-weight: 600; color: __TEXTO__; letter-spacing: -.01em; }
.tr-logo { display: inline-flex; align-items: center; justify-content: center; flex: none;
  background: linear-gradient(135deg, __ACENTO__, __LILA__); }
.tr-seccion { color: __APAGADO__; font-size: .78rem; padding: .2rem .6rem .5rem; }
[data-testid="stSidebar"] [role="radiogroup"] { gap: .15rem; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] {
  padding: .62rem .8rem; border-radius: 12px; width: 100%; cursor: pointer; margin: 0;
  color: __TEXTO__; transition: background .15s;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover { background: rgba(255,255,255,.05); }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] { background: rgba(255,255,255,.10); }
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child { display: none; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] p { font-size: .95rem; display: flex; align-items: center; gap: .7rem; color: __TEXTO__; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] p::before {
  content: ""; width: 19px; height: 19px; flex: none; background-color: currentColor; opacity: .85;
  -webkit-mask: var(--icono) center / contain no-repeat; mask: var(--icono) center / contain no-repeat;
}
.tr-conexion { background: rgba(255,255,255,.04); border: 1px solid __BORDE__; border-radius: 14px;
  padding: .85rem 1rem; margin: 1.6rem 0 .7rem; font-size: .85rem; }
.tr-conexion div { display: flex; align-items: center; gap: .55rem; padding: .18rem 0; color: __TEXTO__; }
.tr-conexion small { color: __APAGADO__; margin-left: auto; font-size: .78rem; }
.tr-punto { width: 8px; height: 8px; border-radius: 50%; background: __VERDE__; flex: none; }

/* tarjetas */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .tr-tarjeta),
div[class*="st-key-tarjeta"] {
  background: __TARJETA__; border: 1px solid __BORDE__; border-radius: 22px; padding: 1.4rem 1.5rem;
}
[data-testid="stVerticalBlockBorderWrapper"] { border-color: __BORDE__ !important; border-radius: 22px; background: __TARJETA__; }
.tr-card-t { font-size: 1.02rem; font-weight: 600; color: __TEXTO__; margin-bottom: .15rem; }
.tr-card-s { font-size: .82rem; color: __APAGADO__; margin-bottom: .6rem; }

/* indicadores (st.metric como tarjetas de color) */
.st-key-kpis [data-testid="stMetric"], .st-key-sync_kpis [data-testid="stMetric"] {
  border-radius: 20px; padding: 1.25rem 1.4rem; background: var(--kpi-bg, #4DA3FF); position: relative; min-height: 112px;
}
[data-testid="stMetric"] [data-testid="stMetricLabel"] p { color: var(--kpi-fg, __TINTA__); font-size: .92rem; font-weight: 500; opacity: .92; }
[data-testid="stMetric"] [data-testid="stMetricValue"] { color: var(--kpi-fg, __TINTA__); font-size: 1.9rem; font-weight: 600; letter-spacing: -.02em; }
.st-key-kpis [data-testid="stMetric"]::after {
  content: ""; position: absolute; top: 1.2rem; right: 1.3rem; width: 24px; height: 24px; background: var(--kpi-fg, __TINTA__); opacity: .85;
  -webkit-mask: var(--icono) center / contain no-repeat; mask: var(--icono) center / contain no-repeat;
}

/* ficha del cliente */
.tr-ficha { display: flex; align-items: center; gap: 1.6rem; flex-wrap: wrap; padding: .4rem .2rem; }
.tr-avatar { width: 92px; height: 92px; border-radius: 50%; flex: none; display: flex; align-items: center; justify-content: center;
  font-size: 2.1rem; font-weight: 600; color: #FFFFFF; background: linear-gradient(135deg, __ACENTO__, __LILA__); letter-spacing: .02em; }
.tr-ficha-datos { flex: 1; min-width: 260px; }
.tr-ficha-nombre { font-size: 2.3rem; font-weight: 600; letter-spacing: -.02em; line-height: 1.15; color: __TEXTO__; }
.tr-ficha-sub { color: __APAGADO__; font-size: 1.05rem; margin: .35rem 0 .8rem; }
.tr-ficha-chips { display: flex; gap: 1.4rem; flex-wrap: wrap; font-size: 1rem; }
.tr-ficha-chips .tr-chip { font-size: 1.02rem; }
.tr-ficha-saldo { text-align: right; padding-left: 1.5rem; }
.tr-ficha-saldo small { display: block; color: __APAGADO__; font-size: .9rem; margin-bottom: .2rem; }
.tr-ficha-saldo b { font-size: 2.6rem; font-weight: 600; letter-spacing: -.02em; color: __TEXTO__; }
.tr-ficha-saldo b.deuda { color: #FFD23F; }

/* tablas propias */
.tr-tabla-wrap { overflow-x: auto; }
.tr-tabla { width: 100%; border-collapse: collapse; font-size: .9rem; }
.tr-tabla th { text-align: left; color: __APAGADO__; font-weight: 500; font-size: .82rem; padding: .7rem .8rem;
  border-bottom: 1px solid __BORDE__; white-space: nowrap; }
.tr-tabla td { padding: .72rem .8rem; border-bottom: 1px solid rgba(255,255,255,.05); color: __TEXTO__; white-space: nowrap; }
.tr-tabla tbody tr:hover td { background: rgba(255,255,255,.035); }
.tr-tabla .r { text-align: right; }
.tr-chip { display: inline-flex; align-items: center; gap: .45rem; font-size: .86rem; }
.tr-chip i { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
__CHIPS__
.tr-vacio { color: __APAGADO__; padding: 1.2rem .4rem; }

/* formularios */
.stApp label p, [data-testid="stWidgetLabel"] p { color: #CFCFCF; font-size: .86rem; }
.stApp input, .stApp textarea, .stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"],
.stApp [data-baseweb="base-input"] {
  background-color: __CAMPO__ !important; color: __TEXTO__ !important; border-color: __BORDE__ !important; border-radius: 12px !important;
}
.stApp [data-baseweb="select"] > div:focus-within, .stApp [data-baseweb="input"]:focus-within { border-color: __ACENTO__ !important; }
.stApp [data-baseweb="select"] span, .stApp [data-baseweb="select"] div { color: __TEXTO__; }
[data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"] { background: #1E1E1E !important; }
[data-baseweb="popover"] li { color: __TEXTO__ !important; }
[data-baseweb="popover"] li:hover { background: rgba(255,255,255,.08) !important; }
.stApp [data-baseweb="tag"] { background: rgba(77,163,255,.22) !important; color: __ACENTO__ !important; border-radius: 8px; }
.stApp [data-baseweb="tag"] span { color: __ACENTO__ !important; }

/* botones */
.stApp button[kind="primary"], .stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-primaryFormSubmit"] {
  background: __ACENTO__; color: __TINTA__; border: 0; border-radius: 12px; font-weight: 600; padding: .55rem 1.3rem;
}
.stApp button[kind="primary"]:hover, .stApp [data-testid="stBaseButton-primary"]:hover,
.stApp [data-testid="stBaseButton-primaryFormSubmit"]:hover { background: #7DBBFF; color: __TINTA__; border: 0; }
.stApp button[kind="primary"] p, .stApp [data-testid="stBaseButton-primary"] p,
.stApp [data-testid="stBaseButton-primaryFormSubmit"] p { color: __TINTA__; font-weight: 600; }
.stApp button[kind="secondary"], .stApp [data-testid="stBaseButton-secondary"] {
  background: rgba(255,255,255,.06); color: __TEXTO__; border: 1px solid __BORDE__; border-radius: 12px;
}
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stBaseButton-secondary"]:hover { border-color: __ACENTO__; color: __ACENTO__; }
.stApp button:disabled { opacity: .45; }

/* colores que Streamlit toma del tema claro del sistema operativo */
[data-testid="stMain"] *, [data-testid="stSidebar"] *, [data-baseweb="popover"] * { border-color: __BORDE__; }
[data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapseButton"] svg,
[data-testid="stExpandSidebarButton"] button, [data-testid="stExpandSidebarButton"] svg { color: __TEXTO__ !important; }
[data-testid="stMainMenu"] button, [data-testid="stMainMenu"] svg { color: __TEXTO__ !important; }
[data-testid="stCode"], [data-testid="stCode"] pre, [data-testid="stCode"] code { background: #0F0F0F !important; }
[data-testid="stCode"] code, [data-testid="stCode"] code span { color: #D8D8D8; }
[data-baseweb="calendar"], [data-baseweb="calendar"] > div { background: #1E1E1E !important; color: __TEXTO__ !important; }
[data-baseweb="calendar"] button, [data-baseweb="calendar"] div[role="gridcell"], [data-baseweb="calendar"] [role="button"] { color: __TEXTO__; }
[data-baseweb="calendar"] select, [data-baseweb="calendar"] [data-baseweb="select"] * { background: transparent !important; color: __TEXTO__ !important; }

[data-testid="stDateInputField"]:focus-within { border-color: __ACENTO__ !important; }
[role="spinbutton"]:focus { background-color: rgba(77,163,255,.40) !important; color: __TEXTO__ !important; }
[role="gridcell"][aria-selected="true"] > div, [role="gridcell"] > div[data-selected="true"] { background-color: __ACENTO__ !important; color: __TINTA__ !important; }

[data-testid="stTextInputRootElement"], [data-testid="stMain"] [role="group"], [data-testid="stNumberInputContainer"],
[data-testid="stSelectbox"] [data-baseweb="select"] > div, [data-testid="stMultiSelect"] [data-baseweb="select"] > div {
  background-color: __CAMPO__ !important; border-color: __BORDE__ !important; color: __TEXTO__;
}
[data-testid="stCheckbox"] label > div:first-of-type { background-color: __CAMPO__; border-color: rgba(255,255,255,.3); }
.tr-tabla th, .tr-tabla td { border: 0; border-bottom: 1px solid rgba(255,255,255,.07); }
.tr-tabla th { border-bottom-color: __BORDE__; }
.tr-tabla, .tr-tabla thead, .tr-tabla tbody, .tr-tabla tr { border: 0 !important; }

/* pestanas, acordeones, avisos, codigo */
[role="tablist"] { gap: 1.4rem; border-bottom: 1px solid __BORDE__; }
[data-testid="stTab"], [data-testid="stTab"] p { color: __APAGADO__; background: transparent; }
[data-testid="stTab"][aria-selected="true"], [data-testid="stTab"][aria-selected="true"] p { color: __TEXTO__; }
[data-testid="stTab"] .react-aria-SelectionIndicator { background-color: __ACENTO__ !important; height: 2px; }

/* controles que Streamlit pinta de rojo por defecto */
.stApp button[data-variant="segmented_control"] {
  background: rgba(255,255,255,.05); color: __TEXTO__; border: 1px solid __BORDE__; border-radius: 10px; min-width: 2.4rem;
}
.stApp button[data-variant="segmented_control"] p { color: inherit; }
.stApp button[data-variant="segmented_control"][aria-checked="true"] { background: __ACENTO__; color: __TINTA__; border-color: __ACENTO__; }
.stApp [data-testid="stCheckbox"] label:has(input:checked) > div:first-of-type { background-color: __ACENTO__ !important; border-color: __ACENTO__ !important; }
.stApp [data-testid="stMain"] [data-testid="stRadioOption"][data-selected="true"] > div > div:first-child > div { background-color: __ACENTO__ !important; }
.stApp [data-testid="stMain"] [data-testid="stRadioOption"][data-selected="true"] > div > div:first-child { background-color: __CAMPO__ !important; border-color: __ACENTO__ !important; }
.stApp [data-testid="stMain"] [data-testid="stRadioOption"] > div > div:first-child { border-color: rgba(255,255,255,.3); }
[data-testid="stExpander"] { border: 1px solid __BORDE__ !important; border-radius: 14px; background: rgba(255,255,255,.025); }
[data-testid="stExpander"] summary { color: #D6D6D6; font-size: .88rem; }
[data-testid="stAlert"] { background: transparent; }
[data-testid="stAlertContainer"] { border-radius: 14px; border: 1px solid __BORDE__; }
[data-testid="stAlertContainer"] > div { background: transparent !important; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) { background: rgba(77,163,255,.15); color: #CFE5FF; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) { background: rgba(53,184,165,.16); color: #C9F2EC; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) { background: rgba(255,210,63,.16); color: #FFE9A3; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) { background: rgba(255,107,87,.16); color: #FFC9C2; }
[data-testid="stAlertContainer"] p { color: inherit; }
[data-testid="stCode"], .stCodeBlock { border-radius: 14px; }

/* paginacion */
.st-key-paginas [role="radiogroup"], .st-key-paginas [data-baseweb="button-group"] { gap: .4rem; }

/* inicio de sesion */
.st-key-login { max-width: 470px; margin: 5vh auto 2rem; background: __TARJETA__; border: 1px solid __BORDE__;
  border-radius: 28px; padding: 2.6rem 2.4rem 2rem; box-shadow: 0 24px 60px rgba(0,0,0,.25); }
.tr-login-cab { text-align: center; margin-bottom: 1.4rem; }
.tr-login-cab .tr-logo { margin-bottom: 1rem; }
.tr-login-cab h2 { font-size: 1.5rem; font-weight: 600; margin: 0 0 .35rem; padding: 0; }
.tr-login-cab p { color: __APAGADO__; font-size: .92rem; margin: 0; }
.tr-sep { display: flex; align-items: center; gap: .7rem; color: #CFCFCF; font-size: .85rem; margin: 1.2rem 0 .5rem; }
.tr-sep::after { content: ""; flex: 1; height: 1px; background: __BORDE__; }
.tr-sep svg { opacity: .9; }
.tr-nota { color: __APAGADO__; font-size: .78rem; text-align: center; margin: 1rem 0 0; }
.st-key-login [data-testid="stForm"] { border: 0; padding: 0; }
.st-key-login [data-testid="stFormSubmitButton"] button { width: 100%; padding: .7rem 1rem; margin-top: .4rem; }
"""


def construir_css(autenticado):
    css = CSS_BASE
    chips = "".join(f".c-{n} {{ color: {c}; }} .c-{n} i {{ background: {c}; }}\n" for n, c in TONOS.items())
    css = css.replace("__CHIPS__", chips)
    for clave, valor in {
        "FONDO": FONDO, "LATERAL": LATERAL, "TARJETA": TARJETA, "CAMPO": CAMPO, "BORDE": BORDE, "TEXTO": TEXTO,
        "APAGADO": APAGADO, "ACENTO": ACENTO, "LILA": LILA, "TINTA": TINTA, "VERDE": TONOS["verde"],
    }.items():
        css = css.replace(f"__{clave}__", valor)
    # iconos del menu lateral (uno por pagina, en el orden de PAGINAS)
    for i, (nombre, (_, icono)) in enumerate(PAGINAS.items(), start=1):
        css += (f'\n[data-testid="stSidebar"] [role="radiogroup"] > div:nth-of-type({i}) '
                f'{{ --icono: url("{svg_uri(icono)}"); }}')
    # iconos de las 4 tarjetas del dashboard
    for i, icono in enumerate(["documento", "moneda", "alerta", "reloj"], start=1):
        css += (f'\n.st-key-kpis [data-testid="stColumn"]:nth-child({i}) [data-testid="stMetric"] '
                f'{{ --icono: url("{svg_uri(icono)}"); }}')
    for i, (fondo, texto) in enumerate(KPI_COLORES, start=1):
        css += (f'\n.st-key-kpis [data-testid="stColumn"]:nth-child({i}) [data-testid="stMetric"], '
                f'.st-key-sync_kpis [data-testid="stColumn"]:nth-child({i}) [data-testid="stMetric"] '
                f'{{ --kpi-bg: {fondo}; --kpi-fg: {texto}; }}')
    if not autenticado:
        css += ('\n[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], '
                '[data-testid="collapsedControl"] { display: none !important; }')
    return css


def inyectar_estilos(autenticado):
    st.markdown(f"<style>{construir_css(autenticado)}</style>", unsafe_allow_html=True)


class Crudo(str):
    """Texto que ya es HTML seguro (no se vuelve a escapar)."""


def esc(valor):
    return html.escape(str(valor)).replace("$", "&#36;")


def chip(texto, tono):
    return Crudo(f'<span class="tr-chip c-{tono}"><i></i>{esc(texto)}</span>')


def chip_estado(estado):
    return chip(estado.capitalize(), ESTADO_TONO.get(estado, "gris"))


def tabla_html(columnas, filas, derecha=()):
    """Tabla con el estilo de la app. Cada celda es texto (se escapa) o un Crudo (chip)."""
    if not filas:
        return '<div class="tr-vacio">Sin resultados.</div>'
    cab = "".join(f'<th class="{"r" if c in derecha else ""}">{esc(c)}</th>' for c in columnas)
    cuerpo = "".join(
        "<tr>" + "".join(
            f'<td class="{"r" if c in derecha else ""}">{v if isinstance(v, Crudo) else esc(v)}</td>'
            for c, v in zip(columnas, fila)) + "</tr>"
        for fila in filas)
    return (f'<div class="tr-tabla-wrap"><table class="tr-tabla"><thead><tr>{cab}</tr></thead>'
            f'<tbody>{cuerpo}</tbody></table></div>')


def encabezado(seccion, titulo, descripcion=""):
    st.markdown(f'<div class="tr-migas">Truck Rental<span>/</span><b>{esc(seccion)}</b></div>', unsafe_allow_html=True)
    st.title(titulo)
    if descripcion:
        st.caption(descripcion)


def titulo_tarjeta(titulo, subtitulo=""):
    sub = f'<div class="tr-card-s">{esc(subtitulo)}</div>' if subtitulo else ""
    st.markdown(f'<div class="tr-card-t">{esc(titulo)}</div>{sub}', unsafe_allow_html=True)


def estilo_grafico(fig, leyenda=False, alto=300):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", height=alto, showlegend=leyenda,
        margin=dict(l=4, r=4, t=6, b=4), font=dict(family="Inter, Segoe UI, sans-serif", color=APAGADO, size=12),
        hoverlabel=dict(bgcolor="#1E1E1E", font_color=TEXTO, bordercolor="#1E1E1E"),
        legend=dict(font=dict(color="#D6D6D6"), bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(title=None, showgrid=False, linecolor=BORDE, tickfont=dict(color=APAGADO))
    fig.update_yaxes(title=None, gridcolor="rgba(255,255,255,0.07)", zeroline=False, tickfont=dict(color=APAGADO),
                     automargin=True, tickformat="~s")
    return fig


def grafico_barras(df, x, y, formato=".3s"):
    fig = px.bar(df, x=x, y=y, color=x, color_discrete_sequence=PALETA, text_auto=formato)
    fig.update_traces(marker_cornerradius=10, textposition="outside", cliponaxis=False,
                      textfont=dict(color=APAGADO, size=11))
    return estilo_grafico(fig)


def grafico_dona(df, nombres, valores):
    fig = px.pie(df, names=nombres, values=valores, hole=0.64, color=nombres,
                 color_discrete_map={"REAL": "#35B8A5", "POR DEFECTO": "#FFD23F"},
                 color_discrete_sequence=PALETA)
    fig.update_traces(textinfo="percent", textfont=dict(color=TINTA, size=12), sort=False,
                      marker=dict(line=dict(color=TARJETA, width=4)))
    fig = estilo_grafico(fig, leyenda=True)
    fig.update_layout(legend=dict(orientation="v", x=1.0, y=0.5, font=dict(color="#D6D6D6")))
    return fig


def mostrar_grafico(fig):
    st.plotly_chart(fig, theme=None, width="stretch", config={"displayModeBar": False})


def paginar(filas, por_pagina=10, clave="pagina_tabla"):
    """Devuelve (filas de la pagina actual, pagina). Dibuja los numeros de pagina como chips."""
    n_pag = max(1, -(-len(filas) // por_pagina))
    pag = 1
    if n_pag > 1:
        with st.container(key="paginas"):
            elegida = st.segmented_control("Página", list(range(1, n_pag + 1)), default=1, key=clave,
                                           label_visibility="collapsed")
        pag = elegida or 1
    ini = (pag - 1) * por_pagina
    return filas[ini:ini + por_pagina], pag, n_pag


# ----------------------------------------------------------------------------
# Paginas
# ----------------------------------------------------------------------------
def pagina_dashboard():
    col = coleccion()
    encabezado("Dashboard", "Dashboard ejecutivo",
               "Todo se calcula en MongoDB Atlas con pipelines de agregación, sin JOIN y en una sola pasada de lectura.")
    total = col.count_documents({})
    if total == 0:
        st.info("Todavía no hay arriendos en MongoDB. Ve a **Sincronización** y "
                "presiona *Sincronizar todo* para copiar los datos desde Oracle.")
        return

    facet = list(col.aggregate(PIPE_FACET))[0]
    ingresos = sum(x["ingresos"] for x in facet["IngresosPorTipoCliente"])
    pend = facet["MultasPendientes"][0] if facet["MultasPendientes"] else {"monto_total": 0, "cantidad": 0}
    with st.container(key="kpis"):
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Arriendos consolidados", f"{total}")
        k2.metric("Ingresos por arriendos", clp(ingresos))
        k3.metric("Multas por cobrar", clp(pend["monto_total"]))
        k4.metric("Multas pendientes", f'{pend["cantidad"]}')
    mostrar_pipeline("Widget 6 · Reporte multi-métrica ($facet): estas 4 tarjetas salen de una sola consulta",
                     PIPE_FACET)

    c1, c2 = st.columns([5, 7])
    with c1:
        with st.container(border=True):
            titulo_tarjeta("Control de garantías", "Arriendos según la garantía aplicada")
            df = a_df(col.aggregate(PIPE_GARANTIAS))
            if not df.empty:
                mostrar_grafico(grafico_dona(df, "Tipo_Garantia", "Cantidad_Arriendos"))
            mostrar_pipeline("Widget 1 · Garantías ($group)", PIPE_GARANTIAS)
    with c2:
        with st.container(border=True):
            titulo_tarjeta("Ingresos por tipo de cliente", "Suma del total de arriendos, en pesos")
            df = a_df(col.aggregate(PIPE_TIPO_CLIENTE)).rename(columns={"_id": "Tipo de cliente"})
            if not df.empty:
                mostrar_grafico(grafico_barras(df, "Tipo de cliente", "Ingresos_Generados"))
            mostrar_pipeline("Widget 3 · Ingresos por segmento ($group)", PIPE_TIPO_CLIENTE)

    c3, c4 = st.columns(2)
    with c3:
        with st.container(border=True):
            titulo_tarjeta("Rentabilidad real por camión", "Top 10 por patente: arriendos más multas, en pesos")
            df = a_df(col.aggregate(PIPE_RENTABILIDAD)).rename(columns={"_id": "Patente"}).head(10)
            if not df.empty:
                mostrar_grafico(grafico_barras(df, "Patente", "Ingreso_Total"))
            mostrar_pipeline("Widget 2 · Rentabilidad ($addFields + $group)", PIPE_RENTABILIDAD)
    with c4:
        with st.container(border=True):
            titulo_tarjeta("Días promedio de arriendo por marca", "Promedio de días solicitados")
            df = a_df(col.aggregate(PIPE_DIAS_MARCA)).rename(columns={"_id": "Marca"})
            if not df.empty:
                # un documento antiguo sin fechas.dias_solicitados da promedio nulo: se ignora
                df["PromedioDiasArriendo"] = pd.to_numeric(df["PromedioDiasArriendo"], errors="coerce").round(1)
                df = df.dropna(subset=["PromedioDiasArriendo"])
            if not df.empty:
                mostrar_grafico(grafico_barras(df, "Marca", "PromedioDiasArriendo", formato=True))
            mostrar_pipeline("Widget 5 · Promedio por marca ($avg)", PIPE_DIAS_MARCA)

    with st.container(border=True):
        titulo_tarjeta("Alerta: multas pendientes de cobro", "Multas que todavía no se pagan ni se condonan")
        det = a_df(col.aggregate(PIPE_DETALLE_PEND))
        if det.empty:
            st.success("No hay multas pendientes de cobro.")
        else:
            filas = [[r["_id"], r["cliente"], r["patente"], int(r["id_multa"]), r["tipo"].capitalize(),
                      clp(r["monto"]), chip_estado("PENDIENTE")] for r in det.to_dict("records")]
            st.markdown(tabla_html(["Arriendo", "Cliente", "Patente", "N° multa", "Tipo", "Monto (CLP)", "Estado"],
                                   filas, derecha=("Monto (CLP)",)), unsafe_allow_html=True)
        mostrar_pipeline("Widget 4 · Multas pendientes ($unwind + $match + $group)", PIPE_MULTAS_PEND)


def sin_tildes(texto):
    """Minusculas y sin tildes, para buscar 'munoz' y encontrar 'Muñoz'."""
    base = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in base if not unicodedata.combining(c)).casefold()


def formato_rut(rut):
    r = str(rut)
    return f"{int(r):,}".replace(",", ".") if r.isdigit() else r


def iniciales(nombre):
    partes = [p for p in str(nombre).split() if p]
    if not partes:
        return "?"
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()


def detalle_multa(m):
    det = m.get("detalles", {})
    if "dias_retraso" in det:
        return f'{det["dias_retraso"]} día(s) de retraso'
    return det.get("descripcion") or "—"


def pagina_clientes():
    col = coleccion()
    encabezado("Clientes", "Ficha del cliente",
               "Busca por nombre o RUT y mira su estado, sus arriendos y sus multas en una sola pantalla.")
    lista = list(col.aggregate(PIPE_LISTA_CLIENTES))
    if not lista:
        st.info("Todavía no hay arriendos en MongoDB. Ve a **Sincronización** y "
                "presiona *Sincronizar todo* para copiar los datos desde Oracle.")
        return

    with st.container(border=True):
        a, b = st.columns(2)
        consulta = a.text_input("Buscar cliente", placeholder="Nombre o RUT, por ejemplo: Uval o 10586995")
        palabras = sin_tildes(consulta).split()
        digitos = re.sub(r"\D", "", consulta)
        coincidencias = [
            c for c in lista
            if (not palabras and not digitos)
            or (palabras and all(p in sin_tildes(c.get("nombre", "")) for p in palabras))
            or (digitos and digitos in str(c["_id"]))
        ]
        if not coincidencias:
            b.selectbox("Cliente", [], disabled=True, placeholder="Sin resultados")
        else:
            rut = b.selectbox(f"Cliente ({len(coincidencias)} coincidencia(s))", [c["_id"] for c in coincidencias],
                              format_func=lambda r: next(f"{c['nombre']} · {formato_rut(r)}"
                                                         for c in coincidencias if c["_id"] == r))
    if not coincidencias:
        st.info("No hay clientes que coincidan con la búsqueda.")
        return

    docs = list(col.find({"cliente.rut": rut}).sort("id_oracle", 1))
    nombre = docs[0]["cliente"].get("nombre", rut)
    tipo = docs[0]["cliente"].get("tipo_cliente", "—")
    multas = [(d, m) for d in docs for m in d.get("multas", [])]
    pendientes = [m for _, m in multas if m.get("estado") == "PENDIENTE"]
    por_cobrar = sum(m["monto"] for m in pendientes)
    total = sum(d["finanzas"]["total_arriendo"] for d in docs)
    en_curso = [d for d in docs if d.get("fechas", {}).get("devolucion") is None]
    inicios = [d["fechas"]["inicio"] for d in docs if d.get("fechas", {}).get("inicio")]

    estado = chip("Con multas pendientes", "naranja") if pendientes else chip("Al día", "verde")
    chips = f"{estado}{chip('Arriendo en curso', 'celeste') if en_curso else ''}{chip('Cliente ' + tipo, 'lila')}"
    with st.container(border=True):
        st.markdown(
            f'<div class="tr-ficha"><div class="tr-avatar">{esc(iniciales(nombre))}</div>'
            f'<div class="tr-ficha-datos"><div class="tr-ficha-nombre">{esc(nombre)}</div>'
            f'<div class="tr-ficha-sub">RUT {esc(formato_rut(rut))}</div>'
            f'<div class="tr-ficha-chips">{chips}</div></div>'
            f'<div class="tr-ficha-saldo"><small>Saldo por cobrar</small>'
            f'<b class="{"deuda" if por_cobrar else ""}">{esc(clp(por_cobrar))}</b></div></div>',
            unsafe_allow_html=True)

    with st.container(key="kpis"):
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Arriendos realizados", f"{len(docs)}")
        k2.metric("Total en arriendos", clp(total))
        k3.metric("Multas pendientes", f"{len(pendientes)}")
        k4.metric("Último arriendo", fecha(max(inicios)) if inicios else "—")

    with st.container(border=True):
        titulo_tarjeta("Arriendos del cliente", "Cada fila es un documento de MongoDB")
        filas = [[d["_id"], d["camion"]["patente"], d["camion"]["marca"], fecha(d["fechas"].get("inicio")),
                  fecha(d["fechas"].get("devolucion")), d["fechas"].get("dias_solicitados", "—"),
                  clp(d["finanzas"]["total_arriendo"]),
                  chip("En curso", "celeste") if d["fechas"].get("devolucion") is None else chip("Finalizado", "verde")]
                 for d in docs]
        pagina, n_pag, total_pag = paginar(filas, 8, "pag_cli_arriendos")
        st.markdown(tabla_html(["Arriendo", "Patente", "Marca", "Inicio", "Devolución", "Días", "Total", "Estado"],
                               pagina, derecha=("Días", "Total")), unsafe_allow_html=True)
        if total_pag > 1:
            st.caption(f"Página {n_pag} de {total_pag}")

    with st.container(border=True):
        titulo_tarjeta("Multas del cliente", "Pendientes, pagadas y condonadas")
        if not multas:
            st.success("Este cliente no tiene multas.")
        else:
            filas = [[m["id_multa"], d["_id"], d["camion"]["patente"], m["tipo"].capitalize(), detalle_multa(m),
                      clp(m["monto"]), chip_estado(m["estado"])] for d, m in multas]
            pagina, n_pag, total_pag = paginar(filas, 8, "pag_cli_multas")
            st.markdown(tabla_html(["N° multa", "Arriendo", "Patente", "Tipo", "Detalle", "Monto (CLP)", "Estado"],
                                   pagina, derecha=("Monto (CLP)",)), unsafe_allow_html=True)
            if total_pag > 1:
                st.caption(f"Página {n_pag} de {total_pag}")
    mostrar_pipeline("Clientes · documentos del cliente ($match)", [{"$match": {"cliente.rut": rut}}])


def pagina_nuevo_arriendo():
    encabezado("Nuevo arriendo", "Registrar un arriendo",
               "Se guarda en Oracle (el trigger valida las fechas) y se refleja al tiro en MongoDB.")
    with oracle() as conn:
        camiones = fetch(conn, SQL_CAMIONES)
        clientes = fetch(conn, SQL_CLIENTES)
    cam = {c["nro_patente"]: c for c in camiones}
    cli = {int(c["numrun_cli"]): c for c in clientes}

    with st.container(border=True):
        a, b = st.columns(2)
        patente = a.selectbox("Camión", list(cam), format_func=lambda p:
                              f"{p} · {cam[p]['nombre_marca']} · {cam[p]['nombre_tipo_camion']}")
        run = b.selectbox("Cliente", list(cli), format_func=lambda r:
                          f"{r}-{cli[r]['dvrun_cli']} · {nombre_cliente(cli[r])}")
        c, d, e = st.columns(3)
        inicio = c.date_input("Fecha de inicio", dt.date.today())
        dias = int(d.number_input("Días solicitados", min_value=1, max_value=365, value=3, step=1))
        sin_dev = e.checkbox("Aún no devuelto (sin fecha de devolución)")
        devolucion = None
        if not sin_dev:
            devolucion = st.date_input("Fecha de devolución", inicio + dt.timedelta(days=dias))

        sel = cam[patente]
        garantia = "REAL" if sel["valor_garantia_dia"] is not None else "POR DEFECTO (sin garantía propia)"
        st.info(f"Total estimado: **{clp(int(sel['valor_arriendo_dia']) * dias)}** "
                f"({clp(sel['valor_arriendo_dia'])} por día) · Garantía: **{garantia}**")
        registrar = st.button("Registrar arriendo", type="primary")

    if registrar:
        try:
            with oracle() as conn:
                cur = conn.cursor()
                cur.execute("SELECT seq_arriendo.NEXTVAL FROM dual")
                nuevo_id = int(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO arriendo_camion (id_arriendo, nro_patente, numrun_cli, "
                    "fecha_ini_arriendo, dias_solicitados, fecha_devolucion) "
                    "VALUES (:p_id, :p_pat, :p_run, :p_ini, :p_dias, :p_dev)",
                    {"p_id": nuevo_id, "p_pat": patente, "p_run": int(run),
                     "p_ini": dt.datetime.combine(inicio, dt.time.min), "p_dias": dias,
                     "p_dev": dt.datetime.combine(devolucion, dt.time.min) if devolucion else None},
                )
                conn.commit()
                st.success(f"Arriendo #{nuevo_id} guardado en Oracle.")
                if guardar_en_mongo(lambda: sincronizar_uno(conn, coleccion(), nuevo_id)):
                    st.success("Reflejado en MongoDB (ya aparece en el dashboard).")
        except oracledb.Error as e:
            st.error(error_oracle(e))


def pagina_multas():
    encabezado("Multas", "Gestión de multas",
               "Cada acción se guarda en Oracle y se refleja en MongoDB con los operadores $push y arrayFilters.")
    t1, t2, t3 = st.tabs(["Registrar multa", "Cambiar estado", "Condonar por cliente"])

    with t1:
        with oracle() as conn:
            arriendos = fetch_arriendos(conn)
        if not arriendos:
            st.info("No hay arriendos en Oracle.")
        else:
            arr = {int(a["id_arriendo"]): a for a in arriendos}
            id_arr = st.selectbox("Arriendo", list(arr), format_func=lambda i:
                                  f"#{i} · {arr[i]['nro_patente']} · {nombre_cliente(arr[i])} · "
                                  f"{fecha(arr[i]['fecha_ini_arriendo'])}")
            c1, c2 = st.columns(2)
            tipo = c1.selectbox("Tipo de multa", TIPOS_MULTA)
            monto = int(c2.number_input("Monto (CLP)", min_value=1000, value=10000, step=1000))
            dias_ret = None
            descripcion = None
            if tipo == "RETRASO":
                dias_ret = int(st.number_input("Días de retraso", min_value=1, max_value=365, value=1))
            else:
                descripcion = st.text_input("Detalle", "Traslado a corralón" if tipo == "GRUA" else "")
            if st.button("Registrar multa", type="primary"):
                try:
                    with oracle() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT seq_multa.NEXTVAL FROM dual")
                        id_multa = int(cur.fetchone()[0])
                        cur.execute(
                            "INSERT INTO multa (id_multa, id_arriendo, tipo, monto, estado, "
                            "dias_retraso, descripcion) VALUES (:p_id, :p_arr, :p_tipo, :p_monto, "
                            "'PENDIENTE', :p_dias, :p_desc)",
                            {"p_id": id_multa, "p_arr": id_arr, "p_tipo": tipo, "p_monto": monto,
                             "p_dias": dias_ret, "p_desc": descripcion or None},
                        )
                        conn.commit()
                        st.success(f"Multa #{id_multa} guardada en Oracle.")
                        nueva = multa_doc({"id_multa": id_multa, "tipo": tipo, "monto": monto,
                                           "estado": "PENDIENTE", "dias_retraso": dias_ret,
                                           "descripcion": descripcion})

                        def reflejar():
                            col = coleccion()
                            res = col.update_one({"id_oracle": id_arr}, {"$push": {"multas": nueva}})
                            if res.matched_count == 0:  # el arriendo aun no estaba en Mongo
                                sincronizar_uno(conn, col, id_arr)
                        if guardar_en_mongo(reflejar):
                            st.success("Reflejada en MongoDB ($push).")
                except oracledb.Error as e:
                    st.error(error_oracle(e))

    with t2:
        pend = a_df(coleccion().aggregate(PIPE_DETALLE_PEND))
        if pend.empty:
            st.success("No hay multas pendientes.")
        else:
            filas_multa = {int(r["id_multa"]): r for r in pend.to_dict("records")}
            id_multa = st.selectbox("Multa pendiente", list(filas_multa), format_func=lambda i:
                                    f"#{i} · {filas_multa[i]['tipo']} · {clp(filas_multa[i]['monto'])} · "
                                    f"{filas_multa[i]['cliente']} · {filas_multa[i]['patente']}")
            nuevo = st.radio("Nuevo estado", ["PAGADA", "CONDONADA"], horizontal=True)
            if st.button("Actualizar estado", type="primary"):
                try:
                    with oracle() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE multa SET estado = :p_e WHERE id_multa = :p_id",
                                    {"p_e": nuevo, "p_id": id_multa})
                        conn.commit()
                    st.success(f"Multa #{id_multa} marcada como {nuevo} en Oracle.")
                    if guardar_en_mongo(lambda: coleccion().update_one(
                        {"multas.id_multa": id_multa},
                        {"$set": {"multas.$[m].estado": nuevo}},
                        array_filters=[{"m.id_multa": id_multa}],
                    )):
                        st.success("Reflejado en MongoDB (arrayFilters).")
                except oracledb.Error as e:
                    st.error(error_oracle(e))

    with t3:
        cp = a_df(coleccion().aggregate(PIPE_CLIENTES_PEND))
        if cp.empty:
            st.success("No hay clientes con multas pendientes.")
        else:
            filas_cliente = {r["_id"]: r for r in cp.to_dict("records")}
            rut = st.selectbox("Cliente con multas pendientes", list(filas_cliente), format_func=lambda r:
                               f"{filas_cliente[r]['nombre']} · {filas_cliente[r]['cantidad']} multa(s) · "
                               f"{clp(filas_cliente[r]['total'])}")
            st.caption("Esto condona **todas** las multas pendientes de ese cliente.")
            if st.button("Condonar multas pendientes", type="primary"):
                try:
                    with oracle() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE multa SET estado = 'CONDONADA' WHERE estado = 'PENDIENTE' "
                            "AND id_arriendo IN (SELECT id_arriendo FROM arriendo_camion "
                            "WHERE numrun_cli = :p_run)", {"p_run": int(rut)})
                        n = cur.rowcount
                        conn.commit()
                    st.success(f"{n} multa(s) condonada(s) en Oracle.")
                    if guardar_en_mongo(lambda: coleccion().update_many(
                        {"cliente.rut": rut},
                        {"$set": {"multas.$[m].estado": "CONDONADA"}},
                        array_filters=[{"m.estado": "PENDIENTE"}],
                    )):
                        st.success("Reflejado en MongoDB (updateMany + arrayFilters).")
                except oracledb.Error as e:
                    st.error(error_oracle(e))


def pagina_arriendos():
    col = coleccion()
    encabezado("Arriendos", "Consulta de arriendos", "Lectura desde MongoDB: cada fila es un documento autosuficiente.")
    with st.container(border=True):
        f1, f2, f3 = st.columns([2, 2, 2])
        txt = f1.text_input("Patente contiene")
        tipos = f2.multiselect("Tipo de cliente", ["Socio", "VIP", "Nacional", "Extranjero"], placeholder="Todos")
        solo_pend = f3.checkbox("Solo con multas pendientes")

    filtro = {}
    if txt.strip():
        filtro["camion.patente"] = {"$regex": re.escape(txt.strip()), "$options": "i"}
    if tipos:
        filtro["cliente.tipo_cliente"] = {"$in": tipos}
    if solo_pend:
        filtro["multas.estado"] = "PENDIENTE"
    docs = list(col.find(filtro).sort("id_oracle", 1).limit(500))
    st.write(f"**{len(docs)}** arriendo(s) encontrados")
    if not docs:
        return

    filas = []
    for d in docs:
        pend = sum(m["monto"] for m in d.get("multas", []) if m.get("estado") == "PENDIENTE")
        filas.append([
            d["_id"], d["camion"]["patente"],
            d["cliente"].get("nombre", d["cliente"]["rut"]), d["cliente"]["tipo_cliente"],
            fecha(d.get("fechas", {}).get("inicio")), d.get("fechas", {}).get("dias_solicitados", "—"),
            clp(d["finanzas"]["total_arriendo"]), clp(pend) if pend else "—",
            chip("Pendiente", "naranja") if pend else chip("Al día", "verde"),
        ])
    cols = ["Arriendo", "Patente", "Cliente", "Tipo cliente", "Inicio", "Días", "Total arriendo",
            "Por cobrar", "Estado"]
    with st.container(border=True):
        pagina, n_pag, total_pag = paginar(filas, 10)
        st.markdown(tabla_html(cols, pagina, derecha=("Días", "Total arriendo", "Por cobrar")), unsafe_allow_html=True)
        if total_pag > 1:
            st.caption(f"Página {n_pag} de {total_pag}")

    ids = {d["_id"]: d for d in docs}
    elegido = st.selectbox("Ver un arriendo en detalle", list(ids))
    with st.expander("Ver cómo se guarda este arriendo en MongoDB (documento completo)"):
        st.code(json.dumps(ids[elegido], indent=2, ensure_ascii=False, default=str), language="json")

    with st.expander("Eliminar un arriendo"):
        st.warning("Borra el arriendo y sus multas en Oracle y en MongoDB. No se puede deshacer.")
        a_borrar = st.selectbox("Arriendo a eliminar", list(ids), key="borrar")
        ok = st.checkbox("Confirmo que quiero eliminarlo")
        if st.button("Eliminar arriendo", disabled=not ok):
            id_or = ids[a_borrar].get("id_oracle")
            if id_or is None:
                st.error("Este documento no viene de Oracle (dato de prueba). Elimínalo con "
                         "Sincronización → limpiar datos de prueba.")
                return
            try:
                with oracle() as conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM multa WHERE id_arriendo = :p_id", {"p_id": int(id_or)})
                    cur.execute("DELETE FROM arriendo_camion WHERE id_arriendo = :p_id", {"p_id": int(id_or)})
                    conn.commit()
                st.success(f"Arriendo #{id_or} eliminado en Oracle.")
                if guardar_en_mongo(lambda: col.delete_one({"_id": a_borrar})):
                    st.success("Eliminado en MongoDB (deleteOne).")
            except oracledb.Error as e:
                st.error(error_oracle(e))


def pagina_sincronizacion():
    col = coleccion()
    encabezado("Sincronización", "Sincronización Oracle → MongoDB",
               "Oracle es la fuente de verdad. Aquí puedes revisar si ambas bases coinciden y volver a copiar todo.")
    if "resultado_sync" in st.session_state:
        st.success(st.session_state.pop("resultado_sync"))
    with oracle() as conn:
        n_or = fetch(conn, "SELECT COUNT(*) AS n FROM arriendo_camion")[0]["n"]
        m_or = fetch(conn, "SELECT COUNT(*) AS n FROM multa")[0]["n"]
    n_mo = col.count_documents({"origen": "oracle"})
    tot = list(col.aggregate(PIPE_TOTAL_MULTAS))
    m_mo = tot[0]["n"] if tot else 0
    pruebas = col.count_documents({"origen": {"$ne": "oracle"}})

    with st.container(key="sync_kpis"):
        a, b, c, d = st.columns(4)
        a.metric("Arriendos en Oracle", int(n_or))
        b.metric("Arriendos en MongoDB", n_mo + pruebas)
        c.metric("Multas en Oracle", int(m_or))
        d.metric("Multas en MongoDB", m_mo)
    if int(n_or) == n_mo and pruebas == 0 and int(m_or) == m_mo:
        st.success("Ambas bases están en sincronía.")
    else:
        st.warning("Las bases no coinciden todavía. Presiona *Sincronizar todo*.")

    borrar = False
    if pruebas:
        st.info(f"Hay **{pruebas}** documento(s) en MongoDB que no vienen de Oracle "
                "(datos de prueba de la evaluación anterior).")
        borrar = st.checkbox("Eliminar esos documentos de prueba al sincronizar")
    if st.button("Sincronizar todo", type="primary"):
        with st.spinner("Copiando desde Oracle a MongoDB..."):
            with oracle() as conn:
                r = sincronizar_todo(conn, col, borrar)
        st.session_state["resultado_sync"] = (
            f"Listo: {r['total']} arriendos desde Oracle · {r['nuevos']} nuevos · "
            f"{r['actualizados']} actualizados · {r['huerfanos']} ya no existían en Oracle · "
            f"{r['pruebas']} datos de prueba eliminados.")
        st.rerun()


# ----------------------------------------------------------------------------
# Inicio de sesion, barra lateral y arranque
# ----------------------------------------------------------------------------
PAGINAS = {
    "Dashboard": (pagina_dashboard, "panel"),
    "Clientes": (pagina_clientes, "usuario"),
    "Nuevo arriendo": (pagina_nuevo_arriendo, "camion"),
    "Multas": (pagina_multas, "alerta"),
    "Arriendos": (pagina_arriendos, "lista"),
    "Sincronización": (pagina_sincronizacion, "sync"),
}


def separador(icono, texto):
    st.markdown(f'<div class="tr-sep">{svg(icono, 16)}<span>{esc(texto)}</span></div>', unsafe_allow_html=True)


def pantalla_login():
    v = cargar_por_defecto()
    with st.container(key="login"):
        st.markdown(
            f'<div class="tr-login-cab">{logo(54)}<h2>Iniciar sesión</h2>'
            '<p>Conecta Oracle y MongoDB para entrar a Truck Rental</p></div>', unsafe_allow_html=True)
        with st.form("form_conexion", border=False):
            separador("base", "Oracle")
            o_user = st.text_input("Usuario Oracle", v["o_user"], autocomplete="off")
            o_pass = st.text_input("Contraseña Oracle", type="password", autocomplete="new-password")
            separador("base", "MongoDB Atlas")
            m_user = st.text_input("Usuario MongoDB", v["m_user"], autocomplete="off")
            m_pass = st.text_input("Contraseña MongoDB", type="password", autocomplete="new-password")
            with st.expander("Ajustes de conexión"):
                o_host = st.text_input("Host de Oracle", v["o_host"])
                o_port = st.text_input("Puerto de Oracle", v["o_port"])
                o_serv = st.text_input("Servicio de Oracle", v["o_serv"])
                m_host = st.text_input("Servidor de MongoDB", v["m_host"])
                m_db = st.text_input("Base de datos de MongoDB", v["m_db"])
            enviar = st.form_submit_button("Conectar", type="primary", width="stretch")
        st.markdown('<p class="tr-nota">Las contraseñas solo viven en esta sesión: no se guardan en ningún archivo.</p>',
                    unsafe_allow_html=True)
    if enviar:
        if not o_pass or not m_pass:
            st.error("Escribe las dos contraseñas.")
            return
        if not o_user or not m_user:
            st.error("Escribe el usuario de Oracle y el usuario de MongoDB.")
            return
        uri = mongo_uri(m_user, m_pass, m_host)
        with st.spinner("Probando conexiones..."):
            errores = probar_conexiones(o_host, o_port, o_serv, o_user, o_pass, uri)
        if errores:
            for e in errores:
                st.error(e)
            return
        st.session_state["cfg"] = {
            "o_host": o_host, "o_port": o_port, "o_serv": o_serv, "o_user": o_user,
            "o_pass": o_pass, "m_uri": uri, "m_db": m_db, "m_user": m_user,
        }
        st.rerun()


def barra_lateral():
    cfg = st.session_state["cfg"]
    with st.sidebar:
        st.markdown(f'<div class="tr-marca">{logo(38)}<b>Truck Rental</b></div>', unsafe_allow_html=True)
        st.markdown('<div class="tr-seccion">Menú</div>', unsafe_allow_html=True)
        pagina = st.radio("Menú", list(PAGINAS), label_visibility="collapsed")
        st.markdown(
            '<div class="tr-conexion">'
            f'<div><span class="tr-punto"></span>Oracle<small>{esc(cfg["o_user"])} · {esc(cfg["o_serv"])}</small></div>'
            f'<div><span class="tr-punto"></span>MongoDB<small>{esc(cfg["m_db"])}</small></div></div>',
            unsafe_allow_html=True)
        if st.button("Desconectar", width="stretch"):
            del st.session_state["cfg"]
            st.rerun()
    return pagina


def main():
    st.set_page_config(page_title="Truck Rental", page_icon=":material/local_shipping:", layout="wide")
    autenticado = "cfg" in st.session_state
    inyectar_estilos(autenticado)
    if not autenticado:
        pantalla_login()
        return
    pagina = barra_lateral()
    try:
        PAGINAS[pagina][0]()
    except oracledb.Error as e:
        st.error("Oracle: " + error_oracle(e))
    except PyMongoError as e:
        st.error("MongoDB: " + error_mongo(e))


if __name__ == "__main__":
    main()
