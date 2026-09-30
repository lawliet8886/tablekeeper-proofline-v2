FROM python:3.13-slim
WORKDIR /app
COPY app.py /app/app.py
COPY index.html app.js style.css /app/
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "-B", "/app/app.py"]
