(ns kicad-parser.core
  (:gen-class)
  (:require
   [clojure.edn :as edn]
   [clojure.java.io :as io]
   [clojure.string :as str]
   [datascript.core :as d]))

(def schema
  {:component/ref {:db/unique :db.unique/identity}
   :component/attribute {:db/cardinality :db.cardinality/many}
   :attribute/id {:db/unique :db.unique/identity}
   :attribute/component {:db/valueType :db.type/ref}})

(defn- tagged?
  [tag node]
  (and (seq? node)
       (= tag (first node))))

(defn- child
  [tag node]
  (first (filter #(tagged? tag %) (rest node))))

(defn- children
  [tag node]
  (filter #(tagged? tag %) (rest node)))

(defn- child-value
  [tag node]
  (second (child tag node)))

(defn- named-value
  [node]
  [(child-value 'name node)
   (child-value 'value node)])

(defn- field-value
  [node]
  [(child-value 'name node)
   (first (filter string? (rest node)))])

(defn- entries
  [entry-fn nodes]
  (into {}
        (keep (fn [node]
                (let [[k v] (entry-fn node)]
                  (when k
                    [k v]))))
        nodes))

(defn- libsource
  [component]
  (when-let [source (child 'libsource component)]
    {:lib (child-value 'lib source)
     :part (child-value 'part source)
     :description (child-value 'description source)}))

(defn- sheetpath
  [component]
  (when-let [path (child 'sheetpath component)]
    {:names (child-value 'names path)
     :tstamps (child-value 'tstamps path)}))

(defn- component
  [node]
  (let [fields-node (child 'fields node)
        libsource (libsource node)
        sheetpath (sheetpath node)
        tstamps (child-value 'tstamps node)]
    (cond-> {:component/ref (child-value 'ref node)
             :component/value (child-value 'value node)
             :component/footprint (child-value 'footprint node)
             :component/description (child-value 'description node)
             :component/fields (entries field-value (children 'field fields-node))
             :component/properties (entries named-value (children 'property node))}
      libsource (assoc :component/libsource libsource)
      sheetpath (assoc :component/sheetpath sheetpath)
      tstamps (assoc :component/tstamps tstamps))))

(defn parse-components
  [netlist-text]
  (let [netlist (edn/read-string netlist-text)]
    (mapv component (children 'comp (child 'components netlist)))))

(defn- attribute-entities
  [component-ref source entries]
  (for [[name value] entries
        :when (some? value)]
    {:attribute/id (str component-ref ":" source ":" name)
     :attribute/component [:component/ref component-ref]
     :attribute/source source
     :attribute/name name
     :attribute/value value}))

(defn- component-tx
  [{component-ref :component/ref
    fields :component/fields
    properties :component/properties
    :as component}]
  (let [attributes (vec (concat
                         (attribute-entities component-ref "field" fields)
                         (attribute-entities component-ref "property" properties)))]
    (cons (assoc component :component/attribute (mapv #(vector :attribute/id (:attribute/id %)) attributes))
          attributes)))

(defn components->db
  [components]
  (let [conn (d/create-conn schema)]
    (d/transact! conn (mapcat component-tx components))
    @conn))

(defn netlist->db
  [netlist-text]
  (components->db (parse-components netlist-text)))

(defn- executable?
  [path]
  (when path
    (let [file (io/file path)]
      (and (.exists file)
           (.canExecute file)
           (.getPath file)))))

(defn- successful?
  [{:keys [exit]}]
  (zero? exit))

(defn- run-command
  [command]
  (let [process (-> (ProcessBuilder. command)
                    (.redirectErrorStream true)
                    (.start))
        out (slurp (.getInputStream process))
        exit (.waitFor process)]
    {:exit exit
     :out out}))

(defn- flatpak-kicad?
  []
  (successful? (run-command ["flatpak" "info" "org.kicad.KiCad"])))

(defn kicad-cli-command
  []
  (let [os-name (str/lower-case (System/getProperty "os.name"))
        mac-cli (or (executable? "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
                    (executable? "/Applications/KiCad/kicad.app/Contents/MacOS/kicad-cli"))]
    (cond
      (and (str/includes? os-name "linux") (flatpak-kicad?))
      ["flatpak" "run" "--command=kicad-cli" "org.kicad.KiCad"]

      (and (str/includes? os-name "mac") mac-cli)
      [mac-cli]

      :else
      ["kicad-cli"])))

(defn export-netlist
  [schematic-path]
  (let [schematic-file (io/file schematic-path)
        output-dir (or (.getParentFile (.getAbsoluteFile schematic-file))
                       (io/file "."))
        out-file (java.io.File/createTempFile "kicad-parser-" ".net" output-dir)
        command (into (kicad-cli-command)
                      ["sch" "export" "netlist"
                       "--format" "kicadsexpr"
                       "--output" (.getPath out-file)
                       schematic-path])
        {:keys [exit out]} (run-command command)]
    (try
      (when-not (zero? exit)
        (throw (ex-info "KiCad netlist export failed"
                        {:command command
                         :exit exit
                         :output out})))
      (slurp out-file)
      (finally
        (.delete out-file)))))

(defn schematic->db
  [schematic-path]
  (netlist->db (export-netlist schematic-path)))

(defn -main
  [& args]
  (let [[schematic-path] args]
    (when-not schematic-path
      (binding [*out* *err*]
        (println "Usage: clojure -M -m kicad-parser.core path/to/design.kicad_sch"))
      (System/exit 2))
    (print (export-netlist schematic-path))))
