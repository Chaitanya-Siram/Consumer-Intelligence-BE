FROM python:3.11
RUN python -m venv .venv
RUN pip install --upgrade pip
WORKDIR /app
COPY . .
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt
WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]