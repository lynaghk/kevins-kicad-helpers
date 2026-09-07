(ns project-tasks.check-test
  (:require [clojure.test :refer [deftest is]]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(deftest run-stages-passes-when-every-stage-passes
  (is (= {:ok? true :checks [{:label "a" :ok? true}
                             {:label "b" :ok? true}]}
         (check/run-stages [{:label "a" :run! (fn [])}
                            {:label "b" :run! (fn [])}]))))

(deftest run-stages-stops-at-the-first-failure
  (let [ran (atom [])]
    (is (= {:ok? false
            :checks [{:label "a" :ok? true}
                     {:label "b" :ok? false :detail "boom" :report "/r.rpt"}]}
           (check/run-stages
            [{:label "a" :run! #(swap! ran conj "a")}
             {:label "b" :run! #(shared/fail! "boom" {:report "/r.rpt"})}
             {:label "c" :run! #(swap! ran conj "c")}])))
    (is (= ["a"] @ran))))
