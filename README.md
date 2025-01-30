# PowerDNS Setup with PostgreSQL and PowerDNS-Admin

## Description
This setup creates a containerized PowerDNS environment with MariaDB as the backend.

## Repository Structure
```
powerdns/
│── docker-compose.yml
│── Dockerfile
│── pdns.conf
│── entrypoint.sh
│── schema.sql
│── .env
│── README.md
└── db/
    └── init.sql
```

## Prerequisites
- Docker and Docker Compose installed on your machine.
- Docker secrets created for PostgreSQL password and PowerDNS API key.


## Installation
1. Create the secrets:
   ```sh
   mkdir -p secrets
   echo "changeme" > secrets/mysql_root_password.txt
   echo "changeme" > secrets/mysql_password.txt
   ```
2. Start the environment:
   ```sh
   docker-compose up -d
   ```
3. Access the web interface at `http://localhost:8081`

## Cleanup
To stop and remove the containers, network, and volumes, run:
```bash
    docker-compose down -v
```
## License
This repository is licensed under the GPL-3.0 License. See the LICENSE file for more details.