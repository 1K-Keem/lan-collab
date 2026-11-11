from lancollab.client.discovery import DiscoveryClient

with DiscoveryClient() as client:
    server = client.discover()
    print(server)
