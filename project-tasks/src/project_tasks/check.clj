(ns project-tasks.check
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [babashka.process :as p]
            [project-tasks.analysis :as analysis]
            [project-tasks.schematic :as schematic]
            [project-tasks.shared :as shared]))

(defn kicad-check-failure [check-label report]
  {:kkh/error :kicad-check-failure
   :check-label check-label
   :report report})

(defn- kicad-check! [project-dir check-label report args]
  (let [{:keys [exit]} @(p/process (into ["kicad-cli"] args)
                                   {:dir (str project-dir)
                                    :out :string
                                    :err :string})]
    (when-not (zero? exit)
      (shared/fail! (str check-label " failed.")
                    (kicad-check-failure check-label report)))))

(defn run! [repo-dir {:keys [project-dir project-file project-name schematic pcb]}]
  (let [version (shared/build-version repo-dir)
        reports (fs/path project-dir "outputs" version "reports")]

    (fs/create-dirs reports)

    ;; Placeholder so interactive KiCad (which has no -D equivalent) resolves
    ;; ${KKH_VERSION_DATE}; meant to be committed alongside the board.
    (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")

    (let [components (schematic/load-components project-dir schematic)
          findings (analysis/run-checks {:project-dir project-dir
                                         :project-name project-name
                                         :schematic schematic
                                         :pcb pcb
                                         :components components})]
      (when (seq findings)
        (shared/fail!
         (str "Custom PCB checks failed:\n"
              (analysis/format-findings findings)))))

    (shared/shell!
     project-dir
     ["kkh-analyze-schematic" (str schematic)])

    (let [report (fs/path reports (str project-name "-erc.rpt"))]
      (kicad-check!
       project-dir
       "ERC"
       report
       ["sch" "erc"
        "--exit-code-violations"
        "-o" (str report)
        (str schematic)]))

    ;; The real version, not the placeholder, so DRC checks the rendered
    ;; geometry of the text that will actually be plotted.
    (let [report (fs/path reports (str project-name "-drc.rpt"))]
      (kicad-check!
       project-dir
       "DRC"
       report
       ["pcb" "drc"
        "--schematic-parity"
        "--exit-code-violations"
        "-D" (str "KKH_VERSION_DATE=" version)
        "-o" (str report)
        (str pcb)]))))
