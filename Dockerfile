# ChatBI Web 壳容器（本地可跑；SPEC Q1 拍板：mock 库走宿主机 brew PG，不打进镜像）
#
# 构建：docker build -t chatbi-agent .
# 运行：docker run --rm -p 8000:8000 --env-file .env -e PG_DSN_RO="postgresql://chatbi_ro:<密码>@host.docker.internal:5432/chatbi" chatbi-agent
#   （容器内连宿主机 PG 需把 127.0.0.1 换成 host.docker.internal；.env 其余键原样透传）
# 注意：SPEC「PG 部署形态」记录了内网 DNS 封锁容器镜像源——离线环境需先手动导基础镜像。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

RUN pip install --no-cache-dir uv

# 先拷依赖清单再拷代码，充分利用层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# 容器内必须绑 0.0.0.0 才能被宿主机访问（本地直跑仍是 127.0.0.1）
CMD ["python", "-m", "chatbi.web", "--host", "0.0.0.0", "--port", "8000"]
