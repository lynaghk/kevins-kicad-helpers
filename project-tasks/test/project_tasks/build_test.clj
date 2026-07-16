(ns project-tasks.build-test
  (:require [babashka.fs :as fs]
            [clojure.string :as str]
            [clojure.test :refer [deftest is]]
            [project-tasks.build :as build]
            [project-tasks.check :as check]))

(defn with-temp-tree [f]
  (let [root (fs/create-temp-dir)]
    (try
      (f root)
      (finally
        (fs/delete-tree root)))))

(defn spit-pcb [root & copper-layers]
  (let [pcb (fs/path root "board.kicad_pcb")]
    (spit (fs/file pcb)
          (str "(kicad_pcb\n"
               "\t(layers\n"
               (apply str (map-indexed (fn [i layer]
                                         (str "\t\t(" (* 2 i) " \"" layer "\" signal)\n"))
                                       copper-layers))
               "\t\t(25 \"Edge.Cuts\" user)\n"
               "\t)\n"
               ;; Footprint pads carry their own quoted (layers ...) lists,
               ;; which must not be mistaken for the board layer table.
               "\t(footprint \"R_0402\"\n"
               "\t\t(pad \"1\" smd rect\n"
               "\t\t\t(layers \"*.Cu\" \"*.Mask\")\n"
               "\t\t)\n"
               "\t)\n"
               ")\n"))
    pcb))

(deftest inner-copper-layers-of-four-layer-board
  (with-temp-tree
    (fn [root]
      (let [pcb (spit-pcb root "F.Cu" "In1.Cu" "In2.Cu" "B.Cu")]
        (is (= ["In1.Cu" "In2.Cu"] (build/inner-copper-layers pcb)))))))

(deftest inner-copper-layers-of-two-layer-board
  (with-temp-tree
    (fn [root]
      (let [pcb (spit-pcb root "F.Cu" "B.Cu")]
        (is (= [] (build/inner-copper-layers pcb)))))))

(def positions-header "Designator,Mid X,Mid Y,Rotation,Layer")

(defn spit-positions [root & rows]
  (spit (fs/file (fs/path root "positions.csv"))
        (str "﻿" positions-header "\r\n"
             (apply str (map #(str % "\r\n") rows)))))

(defn positions-lines [root]
  (-> (slurp (str (fs/path root "positions.csv")))
      (str/replace-first "﻿" "")
      str/split-lines))

(deftest filter-positions-drops-rows-missing-from-bom
  (with-temp-tree
    (fn [root]
      (spit (fs/file (fs/path root "bom.csv"))
            "﻿Designator,Footprint,Quantity,Value,LCSC Part #\r\n\"R1, R2\",R_0402,2,10k,C25744\r\n")
      (spit-positions root
                      "R1,0.0,0.0,0.0,top"
                      "H1,5.0,5.0,0.0,top"
                      "R2,1.0,1.0,0.0,top")
      (build/filter-positions-to-bom! root)
      (is (= [positions-header
              "R1,0.0,0.0,0.0,top"
              "R2,1.0,1.0,0.0,top"]
             (positions-lines root))))))

(deftest filter-positions-empties-rows-when-bom-missing
  (with-temp-tree
    (fn [root]
      (spit-positions root
                      "E1,0.0,0.0,0.0,top"
                      "J1,0.0,6.25,0.0,top")
      (build/filter-positions-to-bom! root)
      (is (= [positions-header] (positions-lines root))))))

(deftest kicad-check-failure-message-previews-report
  (with-temp-tree
    (fn [root]
      (let [report (fs/path root "outputs" "2026-07-16-b59296" "reports" "piezo_driver-erc.rpt")
            project {:board-name "pcbs/piezo_driver"}]
        (fs/create-dirs (fs/parent report))
        (spit (fs/file report)
              (str "ERC report (2026-07-16T10:59:46, Encoding UTF8)\n"
                   "Report includes: Errors, Warnings\n"
                   "\n"
                   "***** Sheet /\n"
                   "[lib_symbol_mismatch]: Symbol '1206,10uF' doesn't match copy in library 'PCM_JLCPCB-Capacitors'\n"
                   "    ; warning\n"
                   "    @(29.21 mm, 149.86 mm): Symbol C9 [1206,10uF]\n"
                   "[lib_symbol_mismatch]: Symbol '0603,100kΩ' doesn't match copy in library 'PCM_JLCPCB-Resistors'\n"
                   "    ; warning\n"
                   "    @(41.91 mm, 149.86 mm): Symbol R16 [0603,100kΩ]\n"
                   "[lib_symbol_mismatch]: Symbol '0603,10nF' doesn't match copy in library 'PCM_JLCPCB-Capacitors'\n"
                   "    ; warning\n"
                   "    @(41.91 mm, 157.48 mm): Symbol C10 [0603,10nF]\n"
                   "[lib_symbol_mismatch]: Hidden fourth issue.\n"
                   "\n"
                   "** ERC messages: 26  Errors 0  Warnings 26\n"))
        (is (= (str "Could not build pcbs/piezo_driver because ERC failed.\n"
                    "\n"
                    "First errors from:\n"
                    "  outputs/2026-07-16-b59296/reports/piezo_driver-erc.rpt\n"
                    "  ERC messages: 26  Errors 0  Warnings 26\n"
                    "\n"
                    "  1. Symbol '1206,10uF' doesn't match copy in library 'PCM_JLCPCB-Capacitors'\n"
                    "  2. Symbol '0603,100kΩ' doesn't match copy in library 'PCM_JLCPCB-Resistors'\n"
                    "  3. Symbol '0603,10nF' doesn't match copy in library 'PCM_JLCPCB-Capacitors'\n"
                    "\n"
                    "Fix these issues, inspect the report for the complete list, or rerun with --force.")
               (build/format-check-failure root project
                                           (check/kicad-check-failure "ERC" report))))))))

(deftest kicad-check-failure-message-handles-missing-report
  (with-temp-tree
    (fn [root]
      (let [report (fs/path root "outputs" "2026-07-16-b59296" "reports" "piezo_driver-drc.rpt")
            project {:board-name "pcbs/piezo_driver"}]
        (is (= (str "Could not build pcbs/piezo_driver because DRC failed.\n"
                    "\n"
                    "First errors from:\n"
                    "  outputs/2026-07-16-b59296/reports/piezo_driver-drc.rpt\n"
                    "\n"
                    "  Report could not be previewed.\n"
                    "\n"
                    "Fix these issues, inspect the report for the complete list, or rerun with --force.")
               (build/format-check-failure root project
                                           (check/kicad-check-failure "DRC" report))))))))
