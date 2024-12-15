FROM python:3.8

RUN apt-get update && \
    apt-get install -y git && \
    pip install --upgrade pip

RUN git clone https://github.com/PowerDNS-Admin/PowerDNS-Admin.git /opt/webapp && \
    cd /opt/webapp && \
    pip install -r requirements.txt

COPY config.py /opt/webapp/

WORKDIR /opt/webapp

CMD ["gunicorn", "--config", "gunicorn_config.py", "app:create_app()"]
