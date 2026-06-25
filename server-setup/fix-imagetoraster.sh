#!/bin/bash
set -e

echo "Installing img2pdf..."
sudo apt install -y img2pdf

echo "Backing up broken imagetoraster filter..."
sudo mv /usr/lib/cups/filter/imagetoraster /usr/lib/cups/filter/imagetoraster.orig

echo "Creating wrapper filter..."
sudo tee /usr/lib/cups/filter/imagetoraster > /dev/null << 'EOF'
#!/bin/bash
TMPF=$(mktemp /tmp/cups-XXXXXX.pdf)
img2pdf /dev/stdin -o "$TMPF" 2>/dev/null
/usr/lib/cups/filter/pdftoraster "$1" "$2" "$3" "$4" "$5" "$TMPF"
RC=$?
rm -f "$TMPF"
exit $RC
EOF
sudo chmod 755 /usr/lib/cups/filter/imagetoraster

echo "Restarting CUPS..."
sudo systemctl restart cups
sudo cupsenable HP-2515

echo "✅ Done. Image prints now route through PDF conversion."
