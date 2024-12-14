# PowerDNS Setup with PostgreSQL and PowerDNS-Admin

This repository contains all the necessary files to set up a PowerDNS server with a PostgreSQL backend and PowerDNS Admin panel using Docker.

## Repository Structure
powerdns-server/
│
├── .env
├── docker-compose.yml
├── pdns/
│   ├── Dockerfile
│   ├── pdns.conf
│   └── entrypoint.sh
│
├── pdns-admin/
│   ├── Dockerfile
│   └── config.py
│
└── db/
    ├── Dockerfile
    └── init.sql

## Prerequisites
- Docker and Docker Compose installed on your machine.
- Docker secrets created for PostgreSQL password and PowerDNS API key.

## Docker Compose Configuration
The docker-compose.yml file defines the services and configuration for the PowerDNS server, PostgreSQL database, and PowerDNS Admin panel. Ensure the following services are defined:
- pdns: PowerDNS server with PostgreSQL backend.
- db: PostgreSQL database service.
- pdns-admin: PowerDNS Admin panel service.

##Building and Running the Containers
To build and run the containers, navigate to the repository directory and run:
```bash
    docker-compose up -d
```
This command will build the Docker images and start the containers in detached mode.

## Configuration Files
### pdns/pdns.conf
This file contains the configuration for the PowerDNS server.
### pdns-admin/config.py
This file contains the configuration for the PowerDNS Admin panel.

## Cleanup
To stop and remove the containers, network, and volumes, run:
```bash
    docker-compose down -v
```
## License
This repository is licensed under the GPL-3.0 License. See the LICENSE file for more details.