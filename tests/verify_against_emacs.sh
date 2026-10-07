#!/usr/bin/env bash
# Cross-validate the Python EAI/EAP output against the real Emacs
# verilog-mode.  Requires: emacs on PATH, verilog-mode.el (downloaded to
# /tmp/verilog-mode.el if absent).  Exits non-zero on any byte difference.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python3
VM=/tmp/verilog-mode.el
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

[ -f "$VM" ] || curl -sL --max-time 60 -o "$VM" \
    https://raw.githubusercontent.com/veripool/verilog-mode/master/verilog-mode.el

# --- fixture submodule ---
cat > "$WORK/InstModule.v" <<'EOF'
module InstModule (o,i);
   parameter PAR;
   output [31:0] o;
   input i;
   wire [31:0] o = {32{i}};
endmodule
EOF
cat > "$WORK/InstModule2.v" <<'EOF'
module InstModule2 (o,i);
   output [31:0] o;
   input i;
   wire [31:0] o = {32{i}};
endmodule
EOF

# --- cases: name<TAB>extra python flags ---
cat > "$WORK/cases.tsv" <<'EOF'
basic_eai	
template_at	
param_value	--param-value
star	--star-expand --star-save
regexp_filter	
multidim	
EOF

cat > "$WORK/basic_eai.v" <<'EOF'
module top;
   InstModule u_a (/*AUTOINST*/);
endmodule
EOF
cat > "$WORK/template_at.v" <<'EOF'
module top;
   /* InstModule AUTO_TEMPLATE (
       .i (in_sig[@]),
   ); */
   InstModule u_im_2 (/*AUTOINST*/);
endmodule
EOF
cat > "$WORK/param_value.v" <<'EOF'
module top;
   InstModule #(.PAR(9)) u_s (/*AUTOINST*/);
endmodule
EOF
cat > "$WORK/star.v" <<'EOF'
module top;
   InstModule u_s (.*);
endmodule
EOF
cat > "$WORK/regexp_filter.v" <<'EOF'
module top;
   InstModule u_c (/*AUTOINST("i")*/);
endmodule
EOF

# multidimensional ports submodule + case
cat > "$WORK/mm.v" <<'EOF'
module mm (
   input  [3:0][7:0]      packed2,
   input  [7:0]           unpacked_arr [0:3],
   output [1:0][3:0][7:0] packed3,
   input  [7:0]           mixed [0:1][0:2],
   input                  plain
);
endmodule
EOF
cat > "$WORK/multidim.v" <<'EOF'
module top;
   mm u_mm (/*AUTOINST*/);
endmodule
EOF

# emacs driver
cat > "$WORK/run.el" <<'EOF'
(setq verilog-auto-inst-param-value t)
(setq verilog-auto-star-expand t)
(setq verilog-auto-star-save t)
(dolist (f (directory-files default-directory nil "\\.emacs\\.v$"))
  (find-file f) (verilog-mode) (verilog-auto) (save-buffer))
EOF

fail=0
while IFS=$'\t' read -r name flags; do
    src="$WORK/$name.v"
    em="$WORK/$name.emacs.v"
    py="$WORK/$name.python.v"
    cp "$src" "$em"
    # python side (PYTHONPATH so no install needed)
    PYTHONPATH=src "$PY" -m verilog_tooling.inst eai -i "$src" -o "$py" -y "$WORK" $flags
done < "$WORK/cases.tsv"

# --- AUTOINPUT/AUTOOUTPUT scenario (compared whitespace-squeezed: the
# declaration name column is our autodef house style, not emacs's) ---
cat > "$WORK/aio.v" <<'EOF'
module top (
   input ext_clk,
   /*AUTOINPUT*/
   /*AUTOOUTPUT*/
);
   wire decl_o;
   /* InstModule AUTO_TEMPLATE (
       .o (decl_o),
   ); */
   InstModule u_a (/*AUTOINST*/);
   /* InstModule2 AUTO_TEMPLATE (
       .o (mid),
       .i (mid),
   ); */
   InstModule2 u_b (/*AUTOINST*/);
endmodule
EOF
cp "$WORK/aio.v" "$WORK/aio.emacs.v"
PYTHONPATH=src "$PY" -m verilog_tooling.inst eai -i "$WORK/aio.v" -o "$WORK/aio.eai.v" -y "$WORK"
PYTHONPATH=src "$PY" -m verilog_tooling.inout aio -i "$WORK/aio.eai.v" -o "$WORK/aio.python.v" -y "$WORK"

(cd "$WORK" && emacs --batch -l "$VM" -l run.el >/dev/null 2>&1)

while IFS=$'\t' read -r name flags; do
    if diff <(expand -t 8 "$WORK/$name.emacs.v") "$WORK/$name.python.v" >/dev/null 2>&1; then
        echo "IDENTICAL  $name"
    else
        echo "DIFF       $name"
        diff <(expand -t 8 "$WORK/$name.emacs.v") "$WORK/$name.python.v" | head -10
        fail=1
    fi
done < "$WORK/cases.tsv"

# aio: same signal sets/order/comments, house-style columns — compare with
# whitespace runs squeezed and packed ranges removed (emacs only knows a
# width when AUTOINST rewrote the connection as net[width]; ours always
# uses the submodule port width — covered by tests/test_inout.py)
sq() { expand -t 8 "$1" | tr -s ' ' | sed 's/ \[[0-9]*:[0-9]*\]//g'; }
if diff <(sq "$WORK/aio.emacs.v") <(sq "$WORK/aio.python.v") >/dev/null 2>&1; then
    echo "IDENTICAL  aio (squeezed)"
else
    echo "DIFF       aio"
    diff <(sq "$WORK/aio.emacs.v") <(sq "$WORK/aio.python.v") | head -20
    fail=1
fi

exit $fail
