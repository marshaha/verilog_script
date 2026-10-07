"""User-known SystemVerilog interface type names (--interface / vimrc
g:verilog_tooling_interfaces): the interface file is NOT in the -y search
dirs, so the name cannot be discovered by scanning — the port still parses
as an interface port for EAI/AIT, and is never mistaken for a wire
(AW/AREG/ADT/AIO)."""

from verilog_tooling import autodef, inout, inst, wire
from verilog_tooling.inst import parse_module_ports

SUB = """\
module sub (
    input  wire              clk,
    axera_apb_interface.master apb,
    output wire        [7:0] dout
);
endmodule
"""

TOP = """\
module top;
/*AUTOWIRE*/
/*autodef*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .apb  (apb_if),
    .dout (dout_w)
);
endmodule
"""


def mods_with_iface():
    return {
        "sub": parse_module_ports(
            SUB.splitlines(), interfaces={"axera_apb_interface"}
        )
    }


def test_unknown_interface_name_drops_the_port():
    """Without the name being known, the interface port vanishes from the
    parse (the gap --interface fills)."""
    names = [p.name for p in parse_module_ports(SUB.splitlines()).ports]
    assert names == ["clk", "dout"]
    ports = parse_module_ports(
        SUB.splitlines(), interfaces={"axera_apb_interface"}
    ).ports
    apb = [p for p in ports if p.name == "apb"][0]
    assert apb.direction == "interface" and apb.is_interface and apb.modport == "master"


def test_eai_cli_with_interface_flag(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text("module top;\nsub u_sub (/*AUTOINST*/);\nendmodule\n")
    out = tmp_path / "out.v"
    inst.main(
        [
            "eai",
            "-i", str(top),
            "-o", str(out),
            "--ref_file", str(top),
            "-y", str(tmp_path),
            "--interface", "axera_apb_interface",
        ]
    )
    text = out.read_text()
    assert "// Interfaces" in text
    assert ".apb" in text and "apb.master" in text


def test_ait_cli_with_interface_flag(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text("module top;\nsub u_sub (/*autoinst*/);\nendmodule\n")
    out = tmp_path / "out.v"
    inst.main(
        [
            "ait",
            "-i", str(top),
            "-o", str(out),
            "--ref_file", str(top),
            "-y", str(tmp_path),
            "--interface", "axera_apb_interface",
        ]
    )
    text = out.read_text()
    assert ".apb" in text and "apb.master" in text


def test_autowire_ignores_interface_connection():
    """An interface port connection must not become a wire (it is an
    interface instance, not a driven net)."""
    out = wire.auto_wire(TOP.splitlines(), mods_with_iface())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert any("dout_w" in ln for ln in wires)
    assert not any("apb_if" in ln for ln in wires)


def test_autodef_ignores_interface_connection():
    out = autodef.auto_def_t(TOP.splitlines(), mods_with_iface())
    decls = [ln for ln in out if ln.startswith(("wire", "reg"))]
    assert any("dout_w" in ln for ln in decls)
    assert not any("apb_if" in ln for ln in decls)


def test_aio_ignores_interface_connection():
    """AUTOINPUT/AUTOOUTPUT never declare ports for interface connections."""
    out = inout.auto_io(TOP.splitlines(), mods_with_iface())
    decls = [ln for ln in out if ln.startswith(("input", "output"))]
    assert not any("apb_if" in ln for ln in decls)


def test_aall_cli_with_interface_flag(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(TOP.replace("sub u_sub", "sub u_sub"))
    out = tmp_path / "out.v"
    inst.main(
        [
            "aall",
            "-i", str(top),
            "-o", str(out),
            "--ref_file", str(top),
            "-y", str(tmp_path),
            "--interface", "axera_apb_interface",
        ]
    )
    text = out.read_text()
    assert "apb.master" in text  # EAI connected the interface port
    # and neither AW nor AD declared a wire for the interface connection
    decls = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip().startswith(("wire", "reg"))
    ]
    assert not any("apb_if" in ln for ln in decls)
