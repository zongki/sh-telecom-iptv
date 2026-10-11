FROM homeassistant/home-assistant:stable
RUN pip install --no-cache-dir "fastapi<0.100" "pydantic<2" uvicorn
WORKDIR /app
COPY main.py requirements.txt start.sh /app/
COPY iptv /app/iptv
COPY static /app/static
# 监听端口可用 IPTV_PORT 覆盖(与 -p 端口映射的容器侧一致)
ENV IPTV_PORT=8300
EXPOSE 8300
CMD ["sh", "start.sh"]
