# Mobloco

Mobloco - mobile local content

### Requiremets

x86_64 / Ubuntu 26.04 / 2 core / 4GB RAM / 40GB SSD / 10mbps WAN / Open Ports: 4011

### Installation

1. `git clone https://github.com/quasin/mobloco.git mobloco && cd mobloco && bash install.sh`
2. System will reboot.

### Data

`data/<domain>.<ext>` holds each mirrored feed; `data/feeds/<domain>.tsv` holds
`date`, `title`, `link`, `category`, `added` for every entry ever seen in that
feed (new rows only, no duplicates). `date` is the feed's publication time and
`added` the time the row was first saved on this machine (both UTC).
