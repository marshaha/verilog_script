;; gen_golden.el -- batch-generate AUTO-only golden outputs (NO indent step)
;; Mirrors 0test.el's verilog-test-file minus verilog-test-indent-buffer:
;;   delete trailing whitespace -> verilog-delete-auto -> verilog-auto -> untabify
;;
;; Usage (cwd must be the verilog-mode repo root so tests/ resolves):
;;   emacs --batch --no-site-file -l verilog-mode.el \
;;         -l ~/workspace/verilog_script/verify/gen_golden.el \
;;         --eval '(gen-golden "tests/autosense.v" "/tmp/vcmp/golden/autosense.v")'

(setq verilog-library-flags "-I./tests")
;; allow `eval:' file-local variables in batch (tests like read_define_param.v
;; rely on them); matches interactive behavior
(setq enable-local-eval t)

(defun gen-golden (in out)
  "Expand all AUTOs in IN with stock emacs verilog-mode, write AUTO-only result to OUT."
  (let ((buf (find-file-noselect in)))
    (with-current-buffer buf
      (verilog-mode)
      ;; pick up file-local // Local Variables: (reval like verilog-auto does)
      (verilog-auto-reeval-locals)
      ;; delete trailing whitespace (0test.el does this first)
      (goto-char (point-min))
      (while (re-search-forward "[ \t]+$" nil t)
        (replace-match ""))
      ;; delete then expand all autos
      (verilog-delete-auto)
      (verilog-auto)
      (untabify (point-min) (point-max))
      (write-file out)
      (kill-buffer))
    (message "gen-golden: %s -> %s done" in out)))
