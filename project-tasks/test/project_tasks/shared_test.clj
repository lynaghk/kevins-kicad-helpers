(ns project-tasks.shared-test
  (:require [babashka.fs :as fs]
            [cheshire.core :as json]
            [clojure.string :as str]
            [clojure.test :refer [deftest is]]
            [project-tasks.shared :as shared]))

(defn with-temp-tree [f]
  (let [root (fs/create-temp-dir)]
    (try
      (f root)
      (finally
        (fs/delete-tree root)))))

(deftest find-repo-dir-in-repo-root
  (with-temp-tree
    (fn [root]
      (fs/create-dirs (fs/path root "pcbs"))
      (is (= (str root) (some-> (shared/find-repo-dir root) str))))))

(deftest find-repo-dir-walks-up-from-subdir
  (with-temp-tree
    (fn [root]
      (fs/create-dirs (fs/path root "pcbs" "some-board"))
      (fs/create-dirs (fs/path root "firmware" "src"))
      (is (= (str root)
             (some-> (shared/find-repo-dir (fs/path root "pcbs" "some-board")) str)))
      (is (= (str root)
             (some-> (shared/find-repo-dir (fs/path root "firmware" "src")) str))))))

(deftest find-repo-dir-prefers-nearest-ancestor
  (with-temp-tree
    (fn [root]
      (let [inner (fs/path root "nested-project")]
        (fs/create-dirs (fs/path root "pcbs"))
        (fs/create-dirs (fs/path inner "pcbs"))
        (fs/create-dirs (fs/path inner "docs"))
        (is (= (str inner)
               (some-> (shared/find-repo-dir (fs/path inner "docs")) str)))))))

(deftest find-repo-dir-nil-when-no-pcbs-anywhere
  (with-temp-tree
    (fn [root]
      (fs/create-dirs (fs/path root "src"))
      (is (nil? (shared/find-repo-dir (fs/path root "src")))))))

(deftest with-text-variable-defines-variable-during-body
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"meta\": {\"version\": 3}}")
        (shared/with-text-variable
          project-file "KKH_VERSION_DATE" "2026-07-07-393e14-dirty"
          #(is (= "2026-07-07-393e14-dirty"
                  (get-in (json/parse-string (slurp (fs/file project-file)))
                          ["text_variables" "KKH_VERSION_DATE"]))))
        (is (= "{\"meta\": {\"version\": 3}}" (slurp (fs/file project-file))))))))

(deftest with-board-text-variable-updates-cached-property-during-body
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original (str "(kicad_pcb\n"
                          "\t(property \"KKH_VERSION_DATE\" \"0000-00-00-unbuilt\")\n"
                          "\t(gr_text \"${KKH_VERSION_DATE}\")\n"
                          ")\n")]
        (spit (fs/file pcb) original)
        (shared/with-board-text-variable
          pcb "KKH_VERSION_DATE" "2026-07-07-393e14-dirty"
          #(is (str/includes?
                (slurp (fs/file pcb))
                "(property \"KKH_VERSION_DATE\" \"2026-07-07-393e14-dirty\")")))
        (is (= original (slurp (fs/file pcb))))))))

(deftest with-board-text-variable-leaves-board-without-property-alone
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original "(kicad_pcb\n\t(gr_text \"${KKH_VERSION_DATE}\")\n)\n"]
        (spit (fs/file pcb) original)
        (shared/with-board-text-variable
          pcb "KKH_VERSION_DATE" "2026-07-07-393e14"
          #(is (= original (slurp (fs/file pcb)))))
        (is (= original (slurp (fs/file pcb))))))))

(deftest with-board-text-variable-restores-file-on-exception
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original "(kicad_pcb\n\t(property \"KKH_VERSION_DATE\" \"old\")\n)\n"]
        (spit (fs/file pcb) original)
        (is (thrown? Exception
                     (shared/with-board-text-variable
                       pcb "KKH_VERSION_DATE" "new"
                       #(throw (ex-info "boom" {})))))
        (is (= original (slurp (fs/file pcb))))))))

(deftest ensure-text-variable-defines-missing-variable
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"meta\": {\"version\": 3}}")
        (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")
        (is (= "0000-00-00-unbuilt"
               (get-in (json/parse-string (slurp (fs/file project-file)))
                       ["text_variables" "KKH_VERSION_DATE"])))))))

(deftest ensure-text-variable-keeps-existing-value
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")
            original "{\"text_variables\": {\"KKH_VERSION_DATE\": \"custom\"}}"]
        (spit (fs/file project-file) original)
        (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")
        (is (= original (slurp (fs/file project-file))))))))

(deftest with-text-variable-restores-file-on-exception
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"text_variables\": {\"OTHER\": \"kept\"}}")
        (is (thrown? Exception
                     (shared/with-text-variable
                       project-file "KKH_VERSION_DATE" "value"
                       #(throw (ex-info "boom" {})))))
        (is (= "{\"text_variables\": {\"OTHER\": \"kept\"}}"
               (slurp (fs/file project-file))))))))
