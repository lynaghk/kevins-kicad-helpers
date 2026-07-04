(ns project-tasks.build
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [clojure.data.csv :as csv]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(defn- read-csv [path]
  (csv/read-csv (str/replace-first (slurp (str path)) "\uFEFF" "")))

(defn filter-positions-to-bom!
  "Drop rows from positions.csv for parts the BOM does not list, e.g. footprints
  that only have the exclude-from-BOM fabrication attribute set (mounting holes)."
  [toolkit-outputs]
  (let [in-bom (->> (read-csv (fs/path toolkit-outputs "bom.csv"))
                    rest
                    (into #{} (mapcat #(str/split (first %) #",\s*"))))
        positions (fs/path toolkit-outputs "positions.csv")
        [header & rows] (read-csv positions)]
    (with-open [writer (io/writer (fs/file positions))]
      (.write writer "\uFEFF")
      (csv/write-csv writer (cons header (filter (comp in-bom first) rows))))))

(defn run! [repo-dir {:keys [project-dir project-name schematic pcb] :as project} force?]
  (when-not force?
    (try
      (check/run! repo-dir project)
      (catch Exception error
        (shared/fail! (str "Build stopped because PCB checks failed.\n"
                           (.getMessage error)
                           "\nFix the reported issues, or rerun with --force.")))))
  (let [version (shared/build-version repo-dir)
        outputs (fs/path project-dir "outputs" version)
        schematics-outputs (fs/path outputs "schematics")
        models-outputs (fs/path outputs "models")
        production-outputs (fs/path outputs "production")
        toolkit-outputs (fs/path project-dir "production")]
    (fs/delete-tree outputs)
    (fs/create-dirs schematics-outputs)
    (fs/create-dirs models-outputs)
    (fs/create-dirs production-outputs)
    (shared/kicad-cli!
     project-dir
     ["sch" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-sch.pdf")))
      (str schematic)])
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-front.pdf")))
      "-l" "F.Cu,F.Mask,F.Silkscreen,Edge.Cuts,"
      (str pcb)])
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-back.pdf")))
      "--erd"
      "--ev"
      "--mirror"
      "-l" "B.Cu,B.Mask,B.Silkscreen,Edge.Cuts,"
      (str pcb)])
    ;; Emit both the simplified (bounding-box components) and full-fidelity STEP
    ;; models. --keep-full leaves the intermediate full export next to the
    ;; simplified one as <project-name>.full.step.
    (shared/step-export!
     project-dir
     ["--keep-full"
      "-o" (str (fs/path models-outputs (str project-name ".simplified.step")))
      (str pcb)])
    (shared/run-fabrication-toolkit!
     project-dir
     pcb
     ["--autoTranslate"
      "--autoFill"
      "--excludeDNP"
      "--nonInteractive"
      "--noBackup"])
    (filter-positions-to-bom! toolkit-outputs)
    (shared/copy-non-zip-contents! toolkit-outputs production-outputs)
    (fs/copy (fs/path toolkit-outputs "bom.csv")
             (fs/path production-outputs (str project-name "-BOM.csv"))
             {:replace-existing true})
    (fs/copy (fs/path toolkit-outputs "positions.csv")
             (fs/path production-outputs (str project-name "-CPL.csv"))
             {:replace-existing true})
    (fs/copy (shared/production-zip toolkit-outputs project-name)
             (fs/path production-outputs
                      (str project-name "-gerbers-" version ".zip"))
             {:replace-existing true})
    (fs/delete-tree toolkit-outputs)
    (println (str "Version: " version))
    (println (str "Schematic outputs: " schematics-outputs))
    (println (str "Model outputs: " models-outputs))
    (println (str "Production outputs: " production-outputs))))
