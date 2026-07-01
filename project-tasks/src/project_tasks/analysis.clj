(ns project-tasks.analysis
  (:require [clojure.string :as str]
            [project-tasks.schematic :as schematic]))

(defn finding [component message]
  {:reference (:reference component)
   :sheet-name (:sheet-name component)
   :message message})

(defn missing-lcsc-findings [{:keys [components]}]
  (for [component components
        :when (schematic/assembly-part? component)
        :when (str/blank? (get-in component [:fields "LCSC"]))]
    (finding component
             "LCSC field must contain a part number.")))

(def checks
  [{:id :lcsc-part-number
    :run missing-lcsc-findings}])

(defn run-checks [context]
  (->> checks
       (mapcat (fn [{:keys [id run]}]
                 (map #(assoc % :check-id id)
                      (run context))))
       (sort-by (juxt :check-id :reference))
       vec))

(defn display-sheet [{:keys [sheet-name]}]
  (if (str/blank? sheet-name)
    "/"
    sheet-name))

(defn format-findings [findings]
  (str/join
   "\n"
   (for [[check-id check-findings] (group-by :check-id findings)]
     (str (name check-id)
          ":\n"
          (str/join
           "\n"
           (for [{:keys [reference message] :as finding} check-findings]
             (str "  "
                  reference
                  " ("
                  (display-sheet finding)
                  "): "
                  message)))))))
