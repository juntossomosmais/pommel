FROM python:3.14-slim

WORKDIR /app

RUN pip install --upgrade pip && \
    pip install ruff==0.16.3 coverage==7.15.4
