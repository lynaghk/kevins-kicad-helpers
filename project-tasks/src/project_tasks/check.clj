(ns project-tasks.check
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [project-tasks.analysis :as analysis]
            [project-tasks.schematic :as schematic]
            [project-tasks.shared :as shared]))

(defn run! [repo-dir {:keys [project-dir project-name schematic pcb]}]
  (let [version (shared/build-version repo-dir)
        reports (fs/path project-dir "outputs" version "reports")]

    (fs/create-dirs reports)

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

    (shared/kicad-cli!
     project-dir
     ["sch" "erc"
      "--exit-code-violations"
      "-o" (str (fs/path reports (str project-name "-erc.rpt")))
      (str schematic)])

    (shared/kicad-cli!
     project-dir
     ["pcb" "drc"
      "--schematic-parity"
      "--exit-code-violations"
      "-o" (str (fs/path reports (str project-name "-drc.rpt")))
      (str pcb)])))
