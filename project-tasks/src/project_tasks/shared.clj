(ns project-tasks.shared
  (:require [babashka.fs :as fs]
            [babashka.process :as p]
            [cheshire.core :as json]
            [clojure.string :as str])
  (:import [java.time LocalDate]))

(defn fail! [message]
  (throw (ex-info message {})))

(defn find-repo-dir
  "Nearest ancestor of start-dir (inclusive) containing a pcbs/ directory, or nil."
  [start-dir]
  (->> (fs/absolutize start-dir)
       (iterate fs/parent)
       (take-while some?)
       (filter #(fs/directory? (fs/path % "pcbs")))
       first))

(defn repo-dir []
  (or (some-> (find-repo-dir (fs/cwd)) str)
      (fail! (str "Could not find a pcbs/ directory in "
                  (fs/cwd)
                  " or any parent directory."))))

(defn command-result [dir args]
  @(p/process args
              {:dir (str dir)
               :out :string
               :err :string}))

(defn command-succeeds? [dir args]
  (zero? (:exit (command-result dir args))))

(defn command-stdout [dir args]
  (let [{:keys [exit out]} (command-result dir args)]
    (when (zero? exit)
      (str/trim out))))

(defn shell! [dir args]
  (let [{:keys [exit]} @(p/process args
                                   {:dir (str dir)
                                    :inherit true})]
    (when-not (zero? exit)
      (fail! (str "Command failed: " (str/join " " args))))))

(defn current-git-sha [dir]
  (if (command-succeeds? dir ["git" "rev-parse" "--is-inside-work-tree"])
    (or (command-stdout dir ["git" "rev-parse" "--short=6" "HEAD"])
        "nogit")
    "nogit"))

(defn git-working-tree-dirty? [dir]
  (and (command-succeeds? dir ["git" "rev-parse" "--is-inside-work-tree"])
       (or (not (command-succeeds? dir ["git" "diff" "--quiet" "--ignore-submodules" "--"]))
           (not (command-succeeds? dir ["git" "diff" "--cached" "--quiet" "--ignore-submodules" "--"])))))

(defn build-version [repo-dir]
  (cond-> (str (LocalDate/now) "-" (current-git-sha repo-dir))
    (git-working-tree-dirty? repo-dir)
    (str "-dirty")))

(defn project-from-dir [board-dir]
  (let [project-files (->> (fs/glob board-dir "*.kicad_pro")
                           (filter fs/regular-file?)
                           sort
                           vec)]
    (when-not (= 1 (count project-files))
      (fail! (str "Expected exactly one root-level .kicad_pro file in "
                  board-dir
                  ", found "
                  (count project-files)
                  ".")))
    (let [project-file (first project-files)
          project-name (str (fs/strip-ext (fs/file-name project-file)))
          schematic (fs/path board-dir (str project-name ".kicad_sch"))
          pcb (fs/path board-dir (str project-name ".kicad_pcb"))]
      (when-not (fs/regular-file? schematic)
        (fail! (str "Could not find matching schematic: " schematic)))
      (when-not (fs/regular-file? pcb)
        (fail! (str "Could not find matching PCB: " pcb)))
      {:board-name (str (fs/file-name board-dir))
       :project-dir board-dir
       :project-file project-file
       :project-name project-name
       :schematic schematic
       :pcb pcb})))

(defn discover-projects [repo-dir]
  (let [pcbs-dir (fs/path repo-dir "pcbs")]
    (when-not (fs/directory? pcbs-dir)
      (fail! (str "Could not find PCB directory: " pcbs-dir)))
    (->> (fs/list-dir pcbs-dir)
         (filter fs/directory?)
         (filter #(seq (fs/glob % "*.kicad_pro")))
         (map project-from-dir)
         (sort-by :board-name)
         vec)))

(defn kicad-cli! [dir args]
  (shell! dir (into ["kicad-cli"] args)))

(defn with-text-variable
  "Call body-fn with the text variable name=value written into the project
  file, restoring its original contents afterwards. Needed for tools that
  resolve text variables from the .kicad_pro and have no flag to define them
  (kicad-cli takes -D instead)."
  [project-file name value body-fn]
  (let [file (fs/file project-file)
        original (slurp file)]
    (try
      (spit file (-> (json/parse-string original)
                     (assoc-in ["text_variables" name] value)
                     (json/generate-string {:pretty true})))
      (body-fn)
      (finally
        (spit file original)))))

(defn with-board-text-variable
  "Call body-fn with the board file's cached copy of the text variable
  name=value updated, restoring the original contents afterwards. KiCad
  mirrors project text variables into the .kicad_pcb as board-level
  (property ...) entries on save, and pcbnew resolves text from that cached
  copy in preference to the project file, so tools that plot through pcbnew
  (Fabrication Toolkit) see a stale value unless the board copy is updated
  too. Boards whose file lacks the property fall back to the project file
  and are left untouched."
  [pcb name value body-fn]
  (let [file (fs/file pcb)
        original (slurp file)
        pattern (re-pattern (str "(\\(property \""
                                 (java.util.regex.Pattern/quote name)
                                 "\" \")[^\"]*(\"\\))"))
        updated (str/replace original pattern
                             (str "$1"
                                  (java.util.regex.Matcher/quoteReplacement value)
                                  "$2"))]
    (try
      (when-not (= original updated)
        (spit file updated))
      (body-fn)
      (finally
        (when-not (= original updated)
          (spit file original))))))

(defn ensure-text-variable!
  "Persistently define the text variable name=value in the project file unless
  it already defines one. Interactive KiCad has no -D equivalent, so without a
  committed value it reports ${name} references as unresolved; scripted runs
  override this placeholder with the real value."
  [project-file name value]
  (let [file (fs/file project-file)
        parsed (json/parse-string (slurp file))]
    (when-not (contains? (get parsed "text_variables") name)
      (spit file (-> parsed
                     (assoc-in ["text_variables" name] value)
                     (json/generate-string {:pretty true}))))))

(defn run-fabrication-toolkit! [project-dir pcb args]
  (shell! project-dir
          (into ["fabrication-toolkit" "-p" (str pcb)] args)))

(defn step-export! [dir args]
  (shell! dir (into ["kkh-step-export"] args)))

(defn copy-non-zip-contents! [source-dir target-dir]
  (doseq [source (fs/glob source-dir "**")
          :when (not= source source-dir)
          :let [relative (fs/relativize source-dir source)
                target (fs/path target-dir relative)]
          :when (not (and (fs/regular-file? source)
                          (= "zip" (fs/extension source))))]
    (if (fs/directory? source)
      (fs/create-dirs target)
      (do
        (fs/create-dirs (fs/parent target))
        (fs/copy source target)))))

(defn production-zip [production-dir project-name]
  (let [preferred (fs/path production-dir (str project-name ".zip"))]
    (if (fs/regular-file? preferred)
      preferred
      (or (->> (fs/glob production-dir "*.zip")
               (filter fs/regular-file?)
               sort
               first)
          (fail! "Fabrication Toolkit did not create a production Gerber archive.")))))
