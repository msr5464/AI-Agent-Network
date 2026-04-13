package automation.modules.demo;

public class DemoBuilder {

    private String name;

    public DemoBuilder withName(String name) {
        this.name = name;
        return this;
    }

    public DemoBuilder withDefaults() {
        if (this.name == null) {
            this.name = "default";
        }
        return this;
    }

    public DemoData build() {
        withDefaults();
        return new DemoData(name);
    }
}
