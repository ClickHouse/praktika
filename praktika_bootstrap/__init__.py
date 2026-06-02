from pathlib import Path


# Compatibility shim for in-repo imports and unittest discovery.
# The actual bootstrap source of truth lives under bootstrap/src/.
_IMPL_PATH = (
    Path(__file__).resolve().parents[1] / "bootstrap" / "src" / "praktika_bootstrap"
)
if _IMPL_PATH.is_dir():
    __path__.append(str(_IMPL_PATH))
