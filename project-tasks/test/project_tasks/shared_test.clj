(ns project-tasks.shared-test
  (:require [babashka.fs :as fs]
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
