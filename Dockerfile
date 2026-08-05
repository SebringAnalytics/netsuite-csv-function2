FROM mcr.microsoft.com/azure-functions/python:4-python3.10

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg unixodbc unixodbc-dev \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor \
       | tee /usr/share/keyrings/microsoft-prod.gpg > /dev/null \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt
COPY . /home/site/wwwroot
