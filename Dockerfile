# 使用官方輕量 Python 3.11 映像檔
FROM python:3.11-slim

# 避免 Python 寫入 .pyc 並開啟 unbuffered 輸出，利於 Docker 日誌即時顯示
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

# 安裝系統必要工具 (git 用於程式碼鏡像 clone/pull、tzdata 用於時區校準)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 設定容器內工作目錄
WORKDIR /app

# 先複製 requirements.txt 利用 Docker 快取層加速後續構建
COPY requirements.txt .

# 安裝 Python 依賴套件
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式核心代碼 (projects.json 走 docker-compose volume 掛載，不寫死在鏡像)
COPY main.py ./
COPY core/ ./core/
COPY agents/ ./agents/
COPY skills/ ./skills/
COPY tools/ ./tools/
COPY knowledge_base/ ./knowledge_base/

# 建立 reports 與 repos 存放目錄
RUN mkdir -p /app/reports /app/repos

# 暴露服務埠號 (預設 8300)
EXPOSE 8300

# 健康檢查
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8300/health || exit 1

# 預設啟動指令：以 web 模式運行
CMD ["python", "main.py", "--mode", "web", "--host", "0.0.0.0", "--port", "8300"]
