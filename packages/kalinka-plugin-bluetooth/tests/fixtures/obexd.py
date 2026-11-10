"""Private-bus OBEX stand-in; deliberately never opens the system bus."""

import asyncio
import json
import os
from pathlib import Path

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus


async def main():
    bus = await MessageBus(bus_address=os.environ["DBUS_SESSION_BUS_ADDRESS"]).connect()
    directory = Path(os.environ["XDG_CACHE_HOME"])
    session = "/org/bluez/obex/client/session0"
    transfer = session + "/transfer0"
    image = b"\x89PNG\r\n\x1a\ncover"

    def reply(message):
        if message.message_type != MessageType.METHOD_CALL:
            return
        if message.member == "CreateSession":
            address, args = message.body
            directory.joinpath("request.json").write_text(
                json.dumps(
                    {
                        "address": address,
                        **{key: value.value for key, value in args.items()},
                    }
                )
            )
            if args["PSM"].value == 4099:
                return Message.new_error(
                    message, "org.bluez.obex.Error.Failed", "No cover art"
                )
            return Message.new_method_return(message, "o", [session])
        if message.member == "Properties":
            return Message.new_method_return(
                message,
                "aa{sv}",
                [
                    [
                        {
                            "type": Variant("s", "native"),
                            "encoding": Variant("s", "PNG"),
                            "pixel": Variant("s", "600*600"),
                            "size": Variant("t", len(image)),
                        }
                    ]
                ],
            )
        if message.member == "Get":
            target, handle, description = message.body
            assert handle == "0000001"
            assert description["encoding"].value == "PNG"
            Path(target).write_bytes(image)
            # Emit completion before replying, then behave as though the
            # transfer was removed, as obexd does when its callback finishes.
            bus.send(
                Message.new_signal(
                    transfer,
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    "sa{sv}as",
                    [
                        "org.bluez.obex.Transfer1",
                        {
                            "Status": Variant("s", "complete"),
                            "Transferred": Variant("t", len(image)),
                        },
                        [],
                    ],
                )
            )
            return Message.new_method_return(
                message,
                "oa{sv}",
                [
                    transfer,
                    {
                        "Status": Variant("s", "active"),
                        "Size": Variant("t", len(image)),
                    },
                ],
            )
        if message.member == "RemoveSession":
            assert message.body == [session]
            directory.joinpath("removed").touch()
            return Message.new_method_return(message)
        if message.member == "DropSession":
            bus.send(
                Message.new_signal(
                    "/",
                    "org.freedesktop.DBus.ObjectManager",
                    "InterfacesRemoved",
                    "oas",
                    [session, ["org.bluez.obex.Session1", "org.bluez.obex.Image1"]],
                )
            )
            return Message.new_method_return(message)
        return Message.new_error(
            message, "org.freedesktop.DBus.Error.UnknownObject", "Gone"
        )

    bus.add_message_handler(reply)
    await bus.request_name("org.bluez.obex")
    await bus.wait_for_disconnect()


if __name__ == "__main__":
    asyncio.run(main())
