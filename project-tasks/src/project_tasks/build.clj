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
  that only have the exclude-from-BOM fabrication attribute set (mounting holes).
  The Fabrication Toolkit writes no bom.csv at all when every footprint is
  excluded (nothing to assemble), so a missing file means an empty BOM."
  [toolkit-outputs]
  (let [bom (fs/path toolkit-outputs "bom.csv")
        in-bom (if (fs/exists? bom)
                 (->> (read-csv bom)
                      rest
                      (into #{} (mapcat #(str/split (first %) #",\s*"))))
                 #{})
        positions (fs/path toolkit-outputs "positions.csv")
        [header & rows] (read-csv positions)]
    (with-open [writer (io/writer (fs/file positions))]
      (.write writer "\uFEFF")
      (csv/write-csv writer (cons header (filter (comp in-bom first) rows))))))

(defn inner-copper-layers
  "Inner copper layer names from the PCB's board layer table, in stackup order.
  Matching the numeric-id tuple form (<id> \"In<n>.Cu\" signal) keeps footprint
  pads' quoted (layers ...) lists from matching."
  [pcb]
  (into [] (map second) (re-seq #"\(\d+ \"(In\d+\.Cu)\"" (slurp (str pcb)))))

(defn run! [repo-dir {:keys [project-dir project-file project-name schematic pcb] :as project} force?]
  (when-not force?
    (try
      (check/run! repo-dir project)
      (catch Exception error
        (shared/fail! (str "Build stopped because PCB checks failed.\n"
                           (.getMessage error)
                           "\nFix the reported issues, or rerun with --force.")))))
  (let [version (shared/build-version repo-dir)
        ;; Lets boards stamp the build version on the silkscreen by placing a
        ;; ${KKH_VERSION_DATE} text item.
        version-var (str "KKH_VERSION_DATE=" version)
        outputs (fs/path project-dir "outputs" version)
        schematics-outputs (fs/path outputs "schematics")
        toolkit-outputs (fs/path project-dir "production")]
    (fs/delete-tree outputs)
    (fs/create-dirs schematics-outputs)
    (shared/kicad-cli!
     project-dir
     ["sch" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-sch.pdf")))
      (str schematic)])
    ;; Autoscale (--scale 0) centers the board on the page; boards drawn off the
    ;; page origin (e.g. to line up DXF imports) would otherwise plot outside
    ;; the page box and get cropped.
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-front.pdf")))
      "--scale" "0"
      "-D" version-var
      "-l" "F.Cu,F.Mask,F.Silkscreen,Edge.Cuts,"
      (str pcb)])
    (doseq [layer (inner-copper-layers pcb)]
      (shared/kicad-cli!
       project-dir
       ["pcb" "export" "pdf"
        "-o" (str (fs/path schematics-outputs
                           (str project-name "-pcb-"
                                (str/lower-case (fs/strip-ext layer)) ".pdf")))
        "--scale" "0"
        "-D" version-var
        "-l" (str layer ",Edge.Cuts,")
        (str pcb)]))
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-back.pdf")))
      "--scale" "0"
      "--erd"
      "--ev"
      "--mirror"
      "-D" version-var
      "-l" "B.Cu,B.Mask,B.Silkscreen,Edge.Cuts,"
      (str pcb)])
    ;; Emit both the simplified (bounding-box components) and full-fidelity STEP
    ;; models. --keep-full leaves the intermediate full export next to the
    ;; simplified one as <project-name>.full.step.
    (shared/step-export!
     project-dir
     ["--keep-full"
      "-o" (str (fs/path outputs (str project-name ".simplified.step")))
      (str pcb)])
    ;; Fabrication Toolkit resolves text variables through pcbnew and has no
    ;; -D equivalent, so define the variable in the project file while it
    ;; plots. pcbnew prefers the board file's cached (property ...) copy of
    ;; the variable over the project file, so update that too.
    (shared/with-text-variable
      project-file "KKH_VERSION_DATE" version
      #(shared/with-board-text-variable
         pcb "KKH_VERSION_DATE" version
         (fn []
           (shared/run-fabrication-toolkit!
            project-dir
            pcb
            ["--autoTranslate"
             "--autoFill"
             "--excludeDNP"
             "--nonInteractive"]))))
    (filter-positions-to-bom! toolkit-outputs)
    (shared/copy-non-zip-contents! toolkit-outputs outputs)
    (fs/copy (shared/production-zip toolkit-outputs project-name)
             (fs/path outputs (str project-name "-gerbers-" version ".zip")))
    (fs/delete-tree toolkit-outputs)
    (println (str "Version: " version))
    (println (str "Outputs: " outputs))))
