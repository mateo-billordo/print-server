#!/bin/bash
# Setup CUPS, HPLIP, and configure the HP DeskJet 2515 printer on Ubuntu Server.
# Run once on a fresh server.
set -e

echo "=== Installing CUPS and HPLIP ==="
sudo apt update
sudo apt install -y cups hplip cups-client avahi-daemon

echo "=== Adding user to lpadmin group ==="
sudo usermod -aG lpadmin "$USER"

echo "=== Configuring CUPS for network sharing ==="
# Listen on all interfaces
sudo sed -i 's/^Listen localhost:631$/Listen *:631/' /etc/cups/cupsd.conf

# Allow LAN access to web UI and printing
sudo sed -i '/<Location \/>/,/<\/Location>/s/Order allow,deny/Order allow,deny\n  Allow @LOCAL/' /etc/cups/cupsd.conf
sudo sed -i '/<Location \/admin>/,/<\/Location>/s/Order allow,deny/Order allow,deny\n  Allow @LOCAL/' /etc/cups/cupsd.conf

# Enable sharing
sudo cupsctl --share-printers

echo "=== Opening firewall ports ==="
sudo ufw allow 631/tcp comment "CUPS"
sudo ufw allow 5353/udp comment "Avahi/mDNS"

echo "=== Restarting services ==="
sudo systemctl enable --now cups
sudo systemctl enable --now avahi-daemon
sudo systemctl restart cups

echo "=== Setting error policy and ink chip bypass ==="
# retry-current-job: CUPS retries on errors (backend hangs on paper-out, never reports failure)
sudo lpadmin -p HP-2515 -o printer-error-policy=retry-current-job 2>/dev/null || true
# hpPenCheck=0: disables ink level verification (allows printing past "empty" chips after refill)
sudo lpadmin -p HP-2515 -o hpPenCheck=0 2>/dev/null || true

echo ""
echo "✅ CUPS setup complete."
echo ""
echo "Next steps:"
echo "  1. Open http://$(hostname -I | awk '{print $1}'):631 in a browser"
echo "  2. Add the printer via Administration → Add Printer → HP DeskJet 2515 (USB)"
echo "  3. Verify HPLIP backend: lpstat -v HP-2515 (should show hp:/usb/...)"
echo "  4. Run fix-imagetoraster.sh to patch the broken image filter"
echo ""
echo "Printer will be accessible at: ipp://$(hostname -I | awk '{print $1}'):631/printers/HP-2515"
