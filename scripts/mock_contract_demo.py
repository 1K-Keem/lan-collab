"""Mock-only smoke scenario: no sockets, no encrypted networking or real chat."""

from lancollab.common.examples import example_messages
from lancollab.common.fake_transport import FakeTransport


def main() -> None:
    messages = example_messages()
    client, server = FakeTransport.pair()

    client.send(messages["auth"])
    assert server.receive() == messages["auth"]

    server.send(messages["auth_ok"])
    assert client.receive() == messages["auth_ok"]

    client.send(messages["private_chat"])
    assert server.receive() == messages["private_chat"]

    server.send(messages["chat_ack"])
    assert client.receive() == messages["chat_ack"]

    client.close()
    server.close()
    print("MOCK ONLY: contract envelopes exchanged through in-memory queues.")
    print("No TCP server, TLS, authentication, routing or file transfer has run.")


if __name__ == "__main__":
    main()
