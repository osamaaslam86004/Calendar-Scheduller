import warnings

warnings.filterwarnings(
    "ignore",
    message="Using httpx with starlette.testclient is deprecated.*",
    category=DeprecationWarning,
)
