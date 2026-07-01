#!/usr/bin/env bb
;; Run `bin/<task>` in every tool folder that has one, e.g. `run-all.bb test`
;; runs dxf-import/bin/test, easyeda-import/bin/test, ... Language agnostic:
;; a stub can be bash, a uv script, whatever — it just has to be executable.
;; Exit codes: 0 = pass, 125 = skip (report but don't fail), anything else = fail.
(require '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[clojure.string :as str])

(def skip-exit-code 125)

(def repo-root (-> *file* fs/canonicalize fs/parent fs/parent))

(let [[task] *command-line-args*]
  (when (str/blank? task)
    (println "usage: run-all.bb <task>   (runs <tool>/bin/<task> in each tool folder)")
    (System/exit 2))
  (let [stubs (->> (fs/list-dir repo-root)
                   (filter fs/directory?)
                   (map #(fs/path % "bin" task))
                   (filter fs/executable?)
                   (sort-by str))]
    (when (empty? stubs)
      (println (str "No tool has a bin/" task " entry point."))
      (System/exit 2))
    (let [results (mapv (fn [stub]
                          (let [tool (str (fs/file-name (fs/parent (fs/parent stub))))]
                            (println (str "\n=== " tool " ==="))
                            (let [{:keys [exit]} (p/shell {:dir (str (fs/parent (fs/parent stub)))
                                                           :inherit true
                                                           :continue true}
                                                          (str stub))]
                              [tool (cond (zero? exit) :pass
                                          (= skip-exit-code exit) :skip
                                          :else :fail)])))
                        stubs)]
      (println "\n=== summary ===")
      (doseq [[tool result] results]
        (println (format "%-16s %s" tool (str/upper-case (name result)))))
      (when (some #(= :fail (second %)) results)
        (System/exit 1)))))
