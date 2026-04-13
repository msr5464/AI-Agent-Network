package automation.modules.demo.api;

import automation.core.api.ApiDetails;
import com.google.common.net.HttpHeaders;
import okhttp3.MediaType;

public enum DemoApi implements ApiDetails {

    GET_DEMO("GET", "/demo");

    private final String method;
    private String path;

    DemoApi(String method, String path) {
        this.method = method;
        this.path = path;
    }

    @Override
    public String getMethod() {
        return method;
    }

    @Override
    public String getPath() {
        return path;
    }

    public DemoApi withPath(String param, String value) {
        this.path = this.path.replace("{" + param + "}", value);
        return this;
    }
}
