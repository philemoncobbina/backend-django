from ipaddress import ip_address

def is_public_ip(ip):
    """Check if an IP address is a public IP."""
    ip = ip_address(ip)
    return ip.is_global
