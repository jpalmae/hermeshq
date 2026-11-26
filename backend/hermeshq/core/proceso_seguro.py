import ctypes
import logging
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3

logger = logging.getLogger(__name__)


def proteger_proceso() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.warning("No se pudo marcar el proceso como no volcable (errno %s)", ctypes.get_errno())
            return False
        return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        logger.warning("prctl no disponible; el entorno del proceso sigue legible por el mismo usuario")
        return False


CLAVES_SOLO_DEL_PROCESO = ("HERMESHQ_AGENT_TOKEN",)

ARRANQUE_PROTEGIDO = (
    "import ctypes, runpy, sys\n"
    "ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)\n"
    "try:\n"
    "    import tools.environments.local as entorno_local\n"
    "    entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST = frozenset(\n"
    "        entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST | {'HERMESHQ_AGENT_TOKEN'}\n"
    "    )\n"
    "except ImportError:\n"
    "    pass\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def ocultar_a_procesos_hijos(claves: tuple[str, ...] = CLAVES_SOLO_DEL_PROCESO) -> bool:
    try:
        import tools.environments.local as entorno_local
    except ImportError:
        return False
    entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST = frozenset(entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST | set(claves))
    return True


def _interprete_python(ejecutable: str) -> str | None:
    try:
        with open(ejecutable, "rb") as archivo:
            primera = archivo.readline(256).decode("utf-8", "ignore").strip()
    except OSError:
        return None
    if not primera.startswith("#!"):
        return None
    partes = primera[2:].split()
    if not partes:
        return None
    if partes[0].endswith("/env") and len(partes) > 1:
        partes = partes[1:]
    return partes[0] if "python" in partes[0].rsplit("/", 1)[-1] else None


def comando_protegido(ejecutable: str, *argumentos: str) -> list[str]:
    interprete = _interprete_python(ejecutable)
    if not interprete:
        return [ejecutable, *argumentos]
    return [interprete, "-c", ARRANQUE_PROTEGIDO, ejecutable, *argumentos]
