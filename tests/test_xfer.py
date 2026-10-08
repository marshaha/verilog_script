"""xfer.py: AUTOINOUTMODPORT / AUTOASSIGNMODPORT interface parsing."""

from verilog_tooling.xfer import (
    _parse_modport_items,
    auto_assign_modport,
    parse_interface_info,
)

IFACE = """\
interface bus_if;
    logic       req;
    logic       gnt;
    logic [7:0] data;
    modport master (output req, data, input gnt);
endinterface
""".splitlines()


def test_modport_direction_carries_across_comma_items():
    mp = _parse_modport_items("output req, data, input gnt", "master")
    assert mp.outputs == ("req", "data")  # `data` inherits `output`
    assert mp.inputs == ("gnt",)
    assert mp.inouts == ()


def test_assignmodport_emits_every_modport_signal():
    ifaces = {"bus_if": parse_interface_info(IFACE)}
    lines = [
        "module top;",
        "    bus_if u_bus ();",
        '    /*AUTOASSIGNMODPORT("bus_if", "master", "u_bus")*/',
        "endmodule",
    ]
    out = auto_assign_modport(lines, ifaces)
    text = "\n".join(out)
    assert "assign req = u_bus.req;" in text
    assert "assign data = u_bus.data;" in text  # widened signal, was dropped
    assert "assign u_bus.gnt = gnt;" in text
