# LunaTV-config 本地同步服务镜像
# 构建: docker build -t lunatv-sync .
# 运行: docker run -d -p 8899:8899 -v ./data:/app/data lunatv-sync
FROM python:3.11-slim

WORKDIR /app

# 先装依赖再拷代码, 利用层缓存加速重复构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lunatv_sync.py .

# 缓存目录固定在容器内 /app/data, 由宿主机卷映射持久化
ENV LUNATV_DATA_DIR=/app/data \
    LUNATV_DEFAULT_SOURCE=jin18 \
    LUNATV_PORT=8899 \
    LUNATV_REFRESH_MINUTES=360

EXPOSE 8899
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen(\
'http://127.0.0.1:8899/status', timeout=5).status == 200 else 1)"

CMD ["python", "lunatv_sync.py"]
