# Documentation Index

This directory contains all documentation for the Proxmox VM Automation application.

## Getting Started

### New Users
Start here to get the application up and running:

1. **[../README.md](../README.md)** - Main project overview and quick start
2. **[PRODUCTION_QUICK_START.md](PRODUCTION_QUICK_START.md)** - Quick production deployment
3. **[DEVELOPMENT.md](DEVELOPMENT.md)** - Development environment setup

## Deployment Guides

### Production Deployment
- **[PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md)** - Complete production deployment guide (400+ lines)
  - Prerequisites and setup
  - Security configuration
  - Monitoring and maintenance
  - Backup and recovery procedures
  - Troubleshooting

- **[PRODUCTION_QUICK_START.md](PRODUCTION_QUICK_START.md)** - Production quick reference
  - Common commands
  - Quick troubleshooting
  - Maintenance schedules
  - Performance tuning

### Development
- **[DEVELOPMENT.md](DEVELOPMENT.md)** - Comprehensive development guide
  - Local setup with Docker
  - Hot reload configuration
  - Development workflow
  - Testing procedures
  - Debugging tips

- **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Original developer documentation
  - Code architecture
  - Contributing guidelines

### General Deployment
- **[DEPLOYMENT.md](DEPLOYMENT.md)** - General deployment instructions
  - Docker setup
  - Environment configuration
  - Basic troubleshooting

- **[DOCKER.md](DOCKER.md)** - Docker-specific configuration
  - Container architecture
  - Volume management
  - Networking setup

## Feature Documentation

### Authentication System
- **[AUTHENTICATION.md](AUTHENTICATION.md)** - Authentication system overview
  - Session management
  - Password security
  - Role-based access

- **[USER_AUTH_GUIDE.md](USER_AUTH_GUIDE.md)** - User authentication guide
  - Registration process
  - Login procedures
  - Canvas integration

### Load Balancing
- **[LOAD_BALANCING.md](LOAD_BALANCING.md)** - Load balancing implementation
  - Node selection algorithm
  - Weighted random distribution
  - Resource monitoring

### Deployment Groups
- **[DEPLOYMENT_GROUPS_UPDATE.md](DEPLOYMENT_GROUPS_UPDATE.md)** - Deployment groups feature
  - Configuration in templates.yaml
  - Multi-VM deployments
  - Lifecycle management

### King of the Hill (KOTH)
- **[KING_OF_THE_HILL.md](KING_OF_THE_HILL.md)** - Competitive lab arena mode
  - Admin guide: marking a deployment group as `koth_mode`, creating + deploying a game, adding flags, hide/pause toggles
  - Student guide: scoreboard page, claim-from-box curl snippet, flag submission
  - Scoring algorithm, scheduler lifecycle, security considerations
  - Full HTTP API + file-by-file architecture reference

## Architecture & Technical Details

### System Architecture
- **[NFS_LOCK_FIX.md](NFS_LOCK_FIX.md)** - Template replication strategy
  - Local template copies per node
  - NFS lock contention solution

### Refactoring Documentation
- **[REFACTORING.md](REFACTORING.md)** - Major refactoring decisions
- **[REFACTORING_SUMMARY.md](REFACTORING_SUMMARY.md)** - Refactoring summary
- **[AUTH_IMPLEMENTATION_SUMMARY.md](AUTH_IMPLEMENTATION_SUMMARY.md)** - Authentication implementation

## Quick Reference by Use Case

### "I want to deploy in production"
1. [PRODUCTION_QUICK_START.md](PRODUCTION_QUICK_START.md) - Start here
2. [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) - Full details
3. [DOCKER.md](DOCKER.md) - Docker specifics

### "I want to develop/contribute"
1. [DEVELOPMENT.md](DEVELOPMENT.md) - Development setup
2. [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Code architecture
3. [../README.md](../README.md) - Project overview

### "I need to configure authentication"
1. [AUTHENTICATION.md](AUTHENTICATION.md) - System overview
2. [USER_AUTH_GUIDE.md](USER_AUTH_GUIDE.md) - User guide

### "I'm having issues"
1. [PRODUCTION_QUICK_START.md](PRODUCTION_QUICK_START.md#troubleshooting) - Quick fixes
2. [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md#troubleshooting) - Detailed troubleshooting
3. [DEPLOYMENT.md](DEPLOYMENT.md) - General issues

### "I want to understand the architecture"
1. [LOAD_BALANCING.md](LOAD_BALANCING.md) - Load balancing
2. [NFS_LOCK_FIX.md](NFS_LOCK_FIX.md) - Template strategy
3. [DEPLOYMENT_GROUPS_UPDATE.md](DEPLOYMENT_GROUPS_UPDATE.md) - Deployment groups

## Quick Links

- Main project: [../README.md](../README.md)
- Configuration: [../templates.yaml](../templates.yaml)
- Scripts: [../scripts/](../scripts/)
- Tests: [../tests/](../tests/)

---

**Last Updated:** January 28, 2026
