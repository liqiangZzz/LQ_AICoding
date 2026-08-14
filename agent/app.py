from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from agent.api.routes import router
from agent.core.logging_config import configure_logging
from agent.env_utils import get_env, load_environment

# ---------------------------------------------------------------
# 启动前置初始化
# ---------------------------------------------------------------
# 注意：FastAPI/Uvicorn 是当前项目唯一的服务化入口。
# 这里显式调用 load_environment() 加载 .env 文件中的配置，
# 并调用 configure_logging() 初始化日志系统。
# 这样做的好处是不依赖 langgraph dev 等开发服务器自带的环境加载机制。
# ---------------------------------------------------------------

load_environment()
configure_logging()

app = FastAPI(title='我的AI Coding项目', version='0.1.1')

# ---------------------------------------------------------------
# 配置 CORS 中间件
# ---------------------------------------------------------------
# 允许来自以下前端的跨域请求：
#   - 127.0.0.1:3000 / localhost:3000  （常见 React/Next.js 开发端口）
#   - 127.0.0.1:5173 / localhost:5173  （常见 Vite 开发端口）
# 生产环境中应替换为实际的前端域名。
# ---------------------------------------------------------------
_default_origins = (
    "http://127.0.0.1:3000,http://localhost:3000,"
    "http://127.0.0.1:5173,http://localhost:5173"
)
_allowed_origins = [
    origin.strip()
    for origin in get_env("DASHBOARD_ALLOWED_ORIGINS", _default_origins).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or _default_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
