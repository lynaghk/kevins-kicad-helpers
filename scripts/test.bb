#!/usr/bin/env bb
;; Run every tool's test suite. Add a suite here when a tool grows tests.
(require '[babashka.fs :as fs]
         '[babashka.process :as p])

(def repo-root (-> *file* fs/canonicalize fs/parent fs/parent))

(def suites
  [{:name "dxf-import"
    :dir "dxf-import"
    :requires "uv"
    :cmd ["uv" "run" "test_kicad_dxf_import.py"]}
   {:name "easyeda-import"
    :dir "easyeda-import"
    :requires "uv"
    :cmd ["uv" "run" "test_easyeda_import.py"]}
   {:name "analyzer"
    :dir "analyzer"
    :requires "clojure"
    :cmd ["clojure" "-M:test"]}])

(defn run-suite [{:keys [name dir requires cmd]}]
  (println (str "\n=== " name " ==="))
  (if (and requires (nil? (fs/which requires)))
    (do (println (str "SKIP: `" requires "` not installed"))
        :skip)
    (let [{:keys [exit]} (apply p/shell
                                {:dir (str (fs/path repo-root dir))
                                 :inherit true
                                 :continue true}
                                cmd)]
      (if (zero? exit) :pass :fail))))

(let [results (mapv (fn [suite] [(:name suite) (run-suite suite)]) suites)]
  (println "\n=== summary ===")
  (doseq [[name result] results]
    (println (format "%-16s %s" name (clojure.string/upper-case (clojure.core/name result)))))
  (when (some #(= :fail (second %)) results)
    (System/exit 1)))
