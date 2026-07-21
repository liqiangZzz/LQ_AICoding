from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from agent.core.logging_config import configure_logging
from agent.env_utils import load_environment

# ---------------------------------------------------------------
# 启动前置初始化
# ---------------------------------------------------------------
# 注意：FastAPI/Uvicorn 是本课程版本中唯一的服务化入口。
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
