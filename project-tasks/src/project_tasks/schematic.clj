(ns project-tasks.schematic
  (:require [clojure.data.xml :as xml]
            [clojure.string :as str]
            [project-tasks.shared :as shared]))

(defn child-elements
  ([element]
   (filter map? (:content element)))
  ([element tag]
   (filter #(= tag (:tag %)) (child-elements element))))

(defn child-element [element tag]
  (first (child-elements element tag)))

(defn element-text [element]
  (->> (:content element)
       (filter string?)
       (apply str)))

(defn named-values [element child-tag]
  (->> (child-elements element child-tag)
       (map (fn [child]
              [(get-in child [:attrs :name])
               (element-text child)]))
       (into {})))

(defn component [element]
  (let [fields (named-values (child-element element :fields) :field)
        properties (->> (child-elements element :property)
                        (map (fn [property]
                               [(get-in property [:attrs :name])
                                (get-in property [:attrs :value] "")]))
                        (into {}))
        sheetpath (child-element element :sheetpath)]
    {:reference (get-in element [:attrs :ref])
     :value (element-text (child-element element :value))
     :footprint (element-text (child-element element :footprint))
     :fields fields
     :properties properties
     :dnp? (contains? properties "dnp")
     :sheet-name (get-in sheetpath [:attrs :names] "")
     :sheet-path (get-in sheetpath [:attrs :tstamps] "")}))

(defn parse-export [export]
  (let [document (xml/parse-str export)
        components (child-element document :components)]
    (->> (child-elements components :comp)
         (mapv component))))

(defn assembly-part? [{:keys [dnp? footprint]}]
  (and (not dnp?)
       (not (str/blank? footprint))))

(defn load-components [project-dir schematic]
  (let [{:keys [exit out err]}
        (shared/command-result
         project-dir
         ["kicad-cli"
          "sch" "export" "python-bom"
          "-o" "/dev/stdout"
          (str schematic)])]
    (when-not (zero? exit)
      (shared/fail!
       (str "Could not read schematic component data.\n"
            (str/trim err))))
    (parse-export out)))
