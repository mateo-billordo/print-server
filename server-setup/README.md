# Server Setup

Scripts to configure the print server host (Ubuntu Server with HP DeskJet 2515 USB printer).

## Scripts

### setup-cups.sh

Initial CUPS installation and configuration. Run once on a fresh server.

```bash
bash server-setup/setup-cups.sh
```

What it does:
- Installs CUPS, HPLIP, and Avahi (mDNS/AirPrint discovery)
- Configures CUPS to listen on all interfaces (LAN sharing)
- Opens firewall ports (631/tcp, 5353/udp)
- Sets error policy to `retry-current-job` (backend hangs on paper-out; see [Limitations](../README.md#limitations))
- Sets `hpPenCheck=0` (disables ink chip "empty" verification for refilled cartridges)

After running, add the printer via the CUPS web UI at `http://<server-ip>:631`. Verify the HPLIP backend is active with `lpstat -v HP-2515` (should show `hp:/usb/...`).

### fix-imagetoraster.sh

Fixes the broken `imagetoraster` CUPS filter (cups-filters 2.0.0 bug on Ubuntu 24.04). Without this fix, image print jobs from network clients (AirPrint, IPP) crash the filter chain.

```bash
bash server-setup/fix-imagetoraster.sh
```

What it does:
- Installs `img2pdf` on the host
- Backs up the broken `/usr/lib/cups/filter/imagetoraster`
- Replaces it with a wrapper that converts images to PDF first, then passes through the working `pdftoraster` filter
- Restarts CUPS and re-enables the printer

To revert:
```bash
sudo mv /usr/lib/cups/filter/imagetoraster.orig /usr/lib/cups/filter/imagetoraster
sudo systemctl restart cups
```
