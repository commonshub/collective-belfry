//! Which events a connection of an outside app is never given a signature for.
//!
//! One rule set in two places: `services/onboarding/belfry_onboarding/remoteapps.py`
//! (`_host`, `_tag`, `_forbidden` with no perms, the kind check of `allowed`, `own_hosts`) and
//! this file. Both pass `vectors/limits.json`. A connection made with `"scope": "outside"` is
//! refused `sign_event` for:
//!
//! - NIP-98 (27235) without a `u` tag, with one that has no host, or for one of the belfry's
//!   own hosts (the API hosts, the domain and everything under it): the API acts on that key;
//! - NIP-42 AUTH (22242) without a `relay` tag or for one of the belfry's own relays;
//! - NIP-46 (24133);
//! - NIP-29 moderation and joins (9000 to 9009, 9021, 9022);
//! - Blossom authorization (24242) without a `server` tag or for the belfry's own storage;
//! - the profile (0) and the contact list (3);
//! - app data under the belfry's names (30078 with a `d` tag that starts `belfry:`, trimmed,
//!   any case): the sealed conversations' anchor, the read state, the channel order, …;
//! - a label in one of the belfry's namespaces (1985 with an `L` tag, or the mark of an `l`
//!   tag, that is `belfry` or starts `belfry/`, trimmed, any case): a pin, a proposal's
//!   status, a ticket checked in, a vouch, a button's press;
//! - anything that is not an object with an integer `kind`.
//!
//! Kind 5 (a deletion) stays allowed. And an outside connection decrypts (`nip44_decrypt`,
//! `nip04_decrypt`) only with the member's consent, a flag on the connection
//! (`method_refused`); it always encrypts.
//!
//! `host` follows Python's `urllib.parse.urlsplit(...).hostname` as `_host` uses it. Where the
//! two could differ this side answers "" (no host), which refuses: a bracketed host that is
//! not an IPv6 address, and a host with one of the characters NFKC turns into `/ ? # @ :`
//! (Python raises for those; the list below is those characters, not a normalization).

use std::collections::HashSet;
use std::net::Ipv6Addr;

use serde_json::Value;

/// The text of the bunker's answer to a refused `sign_event`.
pub const REFUSAL: &str = "not allowed for this app";

/// The belfry's own names.
#[derive(Debug, Default, Clone)]
pub struct Own {
    pub domain: String,
    pub api: HashSet<String>,
    pub relays: HashSet<String>,
    pub blossom: HashSet<String>,
}

fn hosts(list: &str) -> HashSet<String> {
    list.split(|c: char| c.is_whitespace() || c == ',')
        .map(host)
        .filter(|h| !h.is_empty())
        .collect()
}

impl Own {
    /// From the configuration: the domain, and lists of hosts or URLs separated by commas or
    /// spaces. With a domain the usual names are added (`own_hosts` in remoteapps.py).
    pub fn from_config(domain: &str, api: &str, relays: &str, blossom: &str) -> Own {
        let domain = py_strip(domain)
            .to_lowercase()
            .trim_end_matches('.')
            .to_string();
        let (mut api, mut relays, mut blossom) = (hosts(api), hosts(relays), hosts(blossom));
        if !domain.is_empty() {
            api.insert(domain.clone());
            for name in ["belfry", "pass", "apps", "chat"] {
                api.insert(format!("{name}.{domain}"));
            }
            for name in ["relay", "groups-relay", "bunker"] {
                relays.insert(format!("{name}.{domain}"));
            }
            blossom.insert(format!("blossom.{domain}"));
        }
        Own {
            domain,
            api,
            relays,
            blossom,
        }
    }
}

/// Python's `str.strip()`: Unicode white space, and the separators U+001C to U+001F.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Characters whose NFKC form holds one of `/ ? # @ :` (Python's `_checknetloc` refuses a
/// network location with one of them).
const NFKC_DELIMITERS: &[char] = &[
    '\u{2047}', '\u{2048}', '\u{2049}', '\u{2100}', '\u{2101}', '\u{2105}', '\u{2106}', '\u{2a74}',
    '\u{fe13}', '\u{fe16}', '\u{fe55}', '\u{fe56}', '\u{fe5f}', '\u{fe6b}', '\u{ff03}', '\u{ff0f}',
    '\u{ff1a}', '\u{ff1f}', '\u{ff20}',
];

/// What Python accepts between the brackets of a host: an IPv6 address (with an optional
/// `%zone`), or an "IPvFuture" literal (`v<hex>.<anything>`).
fn bracketed_ok(inner: &str) -> bool {
    if let Some(rest) = inner.strip_prefix('v') {
        return match rest.split_once('.') {
            Some((hex, tail)) => {
                !hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit()) && !tail.is_empty()
            }
            None => false,
        };
    }
    let address = match inner.split_once('%') {
        Some((_, zone)) if zone.is_empty() || zone.contains('%') => return false,
        Some((address, _)) => address,
        None => inner,
    };
    address.parse::<Ipv6Addr>().is_ok()
}

/// The host of an address or of a bare `host[:port]`, lower case; "" when there is none.
pub fn host(value: &str) -> String {
    let text = py_strip(value).to_lowercase();
    if text.is_empty() {
        return String::new();
    }
    let text = if text.contains("://") {
        text
    } else {
        format!("x://{text}")
    };
    // urlsplit: leading control characters and spaces go, tabs and line ends go everywhere
    let url: String = text
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    // the scheme: a letter, then letters, digits, + - . up to the first colon
    let mut rest = url.as_str();
    if let Some(i) = url.find(':') {
        let scheme = &url[..i];
        let valid = scheme
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic())
            && scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c));
        if valid {
            rest = &url[i + 1..];
        }
    }
    let Some(after) = rest.strip_prefix("//") else {
        return String::new();
    };
    let netloc = &after[..after.find(['/', '?', '#']).unwrap_or(after.len())];
    let (open, close) = (netloc.contains('['), netloc.contains(']'));
    if open != close {
        return String::new();
    }
    if open {
        let inner = netloc.split_once('[').map_or("", |(_, r)| r);
        let inner = inner.split_once(']').map_or(inner, |(h, _)| h);
        if !bracketed_ok(inner) {
            return String::new();
        }
    }
    if netloc.contains(NFKC_DELIMITERS) {
        return String::new();
    }
    // the hostname: after the last @; between the brackets, or up to the first colon
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let name = match hostinfo.split_once('[') {
        Some((_, bracketed)) => bracketed.split_once(']').map_or(bracketed, |(h, _)| h),
        None => hostinfo.split_once(':').map_or(hostinfo, |(h, _)| h),
    };
    name.trim_end_matches('.').to_string()
}

/// The event's tags that are lists; None when the tags cannot be read at all.
fn tags_of(event: &Value) -> Option<Vec<&Vec<Value>>> {
    match event.get("tags") {
        None | Some(Value::Null) => Some(Vec::new()),
        Some(Value::Array(tags)) => Some(tags.iter().filter_map(Value::as_array).collect()),
        // Python reads nothing out of "", 0, false, {}, a text or an object; other values
        // are an error there
        Some(Value::String(_)) | Some(Value::Object(_)) => Some(Vec::new()),
        Some(Value::Bool(false)) => Some(Vec::new()),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => Some(Vec::new()),
        Some(_) => None,
    }
}

/// `belfry` or `belfry/…`, trimmed, any case (`_belfry_label` in remoteapps.py). Only a text
/// can be one: no number, list or object reads as such a name in Python either.
fn belfry_label(namespace: &Value) -> bool {
    namespace.as_str().is_some_and(|n| {
        let n = py_strip(n).to_lowercase();
        n == "belfry" || n.starts_with("belfry/")
    })
}

/// Whether an outside app's connection is refused this method: decrypting without the
/// member's consent (the connection's `decrypt` flag). Everything else is decided elsewhere
/// (`forbidden` for `sign_event`).
pub fn method_refused(method: &str, decrypt: bool) -> bool {
    matches!(method, "nip44_decrypt" | "nip04_decrypt") && !decrypt
}

/// The values of the event's tags called `name`, each as `_host` would read it; None when
/// the tags cannot be read at all.
fn tag_hosts(event: &Value, name: &str) -> Option<Vec<String>> {
    let mut out = Vec::new();
    for tag in tags_of(event)? {
        if tag.len() < 2 || tag[0].as_str() != Some(name) {
            continue;
        }
        out.push(match &tag[1] {
            Value::String(s) => host(s),
            Value::Null => host("None"),
            Value::Bool(b) => host(if *b { "True" } else { "False" }),
            Value::Number(n) => host(&n.to_string()),
            // a list or an object as a tag value: no host
            _ => String::new(),
        });
    }
    Some(out)
}

/// Whether `sign_event` of this template is refused to an outside app's connection.
pub fn forbidden(event: &Value, own: &Own) -> bool {
    if !event.is_object() {
        return true;
    }
    // an integer, and not true or false ("1" and 1.0 are refused too)
    let kind = match event.get("kind") {
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => match n.as_i64() {
            Some(kind) => kind,
            // beyond i64: no rule names it, and no event has such a kind
            None => return false,
        },
        _ => return true,
    };
    let hosts_of = |name: &str| tag_hosts(event, name);
    // the belfry's domain or any host under it (git.<domain>, a private GRASP relay that admits
    // members by AUTH, is in no list: the rule must not depend on one)
    let under_domain = |h: &String| {
        !own.domain.is_empty() && (*h == own.domain || h.ends_with(&format!(".{}", own.domain)))
    };
    match kind {
        27235 => {
            let Some(hosts) = hosts_of("u") else {
                return true;
            };
            hosts.is_empty()
                || hosts
                    .iter()
                    .any(|h| h.is_empty() || own.api.contains(h) || under_domain(h))
        }
        22242 => hosts_of("relay").is_none_or(|hosts| {
            hosts.is_empty()
                || hosts
                    .iter()
                    .any(|h| h.is_empty() || own.relays.contains(h) || under_domain(h))
        }),
        24133 => true,
        9000..=9009 | 9021 | 9022 => true,
        24242 => hosts_of("server").is_none_or(|hosts| {
            hosts.is_empty()
                || hosts
                    .iter()
                    .any(|h| h.is_empty() || own.blossom.contains(h))
        }),
        0 | 3 => true,
        // app data under the belfry's own names is the belfry's protocol
        30078 => tags_of(event).is_none_or(|tags| {
            tags.iter().any(|t| {
                t.len() > 1
                    && t[0].as_str() == Some("d")
                    && t[1]
                        .as_str()
                        .is_some_and(|d| py_strip(d).to_lowercase().starts_with("belfry:"))
            })
        }),
        // labels in the belfry's namespaces are decisions here
        1985 => tags_of(event).is_none_or(|tags| {
            tags.iter().any(|t| {
                (t.len() > 1 && t[0].as_str() == Some("L") && belfry_label(&t[1]))
                    || (t.len() > 2 && t[0].as_str() == Some("l") && belfry_label(&t[2]))
            })
        }),
        // kind 5 (a deletion) stays allowed: relays apply it to the author's own events only
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const VECTORS: &str = include_str!("../vectors/limits.json");

    fn joined(v: &Value) -> String {
        v.as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().unwrap())
            .collect::<Vec<_>>()
            .join(", ")
    }

    pub fn vector_own() -> Own {
        let v: Value = serde_json::from_str(VECTORS).unwrap();
        let own = &v["own"];
        let built = Own::from_config(
            own["domain"].as_str().unwrap(),
            &joined(&own["api"]),
            &joined(&own["relays"]),
            &joined(&own["blossom"]),
        );
        // the file lists the sets whole: the configuration gives exactly them
        for (set, key) in [
            (&built.api, "api"),
            (&built.relays, "relays"),
            (&built.blossom, "blossom"),
        ] {
            let listed: HashSet<String> = own[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|h| h.as_str().unwrap().to_string())
                .collect();
            assert_eq!(*set, listed, "{key}");
        }
        built
    }

    #[test]
    fn the_shared_vectors_pass() {
        let v: Value = serde_json::from_str(VECTORS).unwrap();
        let own = vector_own();
        let cases = v["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 73);
        for case in cases {
            assert_eq!(
                forbidden(&case["event"], &own),
                case["refused"].as_bool().unwrap(),
                "{}",
                case["name"]
            );
        }
        // the decrypt consent: `decrypt` is the connection's flag
        let methods = v["methods"].as_array().unwrap();
        assert_eq!(methods.len(), 7);
        for case in methods {
            assert_eq!(
                method_refused(
                    case["method"].as_str().unwrap(),
                    case["decrypt"].as_bool().unwrap()
                ),
                case["refused"].as_bool().unwrap(),
                "{}",
                case["name"]
            );
        }
    }

    #[test]
    fn the_belfrys_names_and_namespaces_are_told_as_python_tells_them() {
        let own = vector_own();
        let j = |v: Value| forbidden(&v, &own);
        use serde_json::json;
        // only a text names something; other values and short tags are passed over
        assert!(!j(
            json!({"kind": 30078, "tags": [["d"], ["d", 5], ["d", ["belfry:x"]], "d"]})
        ));
        assert!(j(
            json!({"kind": 30078, "tags": [["d", "\u{a0}BELFRY:x "]]})
        ));
        assert!(!j(
            json!({"kind": 30078, "tags": [["D", "belfry:x"], ["e", "belfry:x"]]})
        ));
        assert!(!j(json!({"kind": 30079, "tags": [["d", "belfry:x"]]})));
        assert!(j(
            json!({"kind": 1985, "tags": [["l", "x", " Belfry/pin\n"]]})
        ));
        assert!(!j(
            json!({"kind": 1985, "tags": [["l", "belfry/pin"], ["L", null], ["l", "x", 7]]})
        ));
        assert!(!j(
            json!({"kind": 1985, "tags": [["L", "belfry-ish"], ["L", "mybelfry/x"]]})
        ));
        assert!(!j(
            json!({"kind": 5, "tags": [["e", "00"], ["d", "belfry:sealed"]]})
        ));
        // tags that cannot be read at all: refused, not guessed
        for kind in [30078, 1985] {
            assert!(j(json!({"kind": kind, "tags": 5})), "{kind}");
            assert!(!j(json!({"kind": kind, "tags": "d"})), "{kind}");
            assert!(!j(json!({"kind": kind})), "{kind}");
        }
    }

    #[test]
    fn hosts_are_read_the_way_python_reads_them() {
        for (given, want) in [
            ("", ""),
            ("  ", ""),
            ("Belfry.Test", "belfry.test"),
            ("belfry.test:8443", "belfry.test"),
            ("HTTPS://BELFRY.TEST:443/x?y#z", "belfry.test"),
            ("https://belfry.test./x", "belfry.test"),
            ("https://user:pw@belfry.test/x", "belfry.test"),
            ("https://belfry.test@example.org/x", "example.org"),
            ("https://a@b@belfry.test", "belfry.test"),
            ("wss://[2001:DB8::1]:444/", "2001:db8::1"),
            ("[::1]", "::1"),
            ("https://[fe80::1%eth0]/", "fe80::1%eth0"),
            ("https://[not-an-address]/", ""),
            ("https://[::1/", ""),
            ("https://::1]/", ""),
            ("https://[1.2.3.4]/", ""),
            ("https://[v1.x]/", "v1.x"),
            ("https:///path", ""),
            ("https://?q", ""),
            ("relay", "relay"),
            ("ws://relay:8080", "relay"),
            ("https://bel\tfry.test/", "belfry.test"),
            // no "://" in the text as given: read as a bare host, as Python does
            ("https:/\t/belfry.test/", "https"),
            ("a b://belfry.test", ""),
            ("1x://belfry.test", ""),
            ("https://belfry.test\u{ff0f}x.example.org/", ""),
            ("https://b\u{fc}cher.example/", "b\u{fc}cher.example"),
            ("https://belfry.test\\@example.org/", "example.org"),
            ("//belfry.test/x", ""),
        ] {
            assert_eq!(host(given), want, "{given:?}");
        }
    }

    #[test]
    fn the_configuration_gives_the_usual_names() {
        let own = Own::from_config(
            " Belfry.Test. ",
            "https://console.example.org/x, extra.example.org:8443",
            "ws://relay:8080 wss://other-relay.example:444/",
            "",
        );
        assert_eq!(own.domain, "belfry.test");
        for h in [
            "belfry.test",
            "belfry.belfry.test",
            "pass.belfry.test",
            "apps.belfry.test",
            "chat.belfry.test",
            "console.example.org",
            "extra.example.org",
        ] {
            assert!(own.api.contains(h), "{h}");
        }
        for h in [
            "relay.belfry.test",
            "groups-relay.belfry.test",
            "bunker.belfry.test",
            "relay",
            "other-relay.example",
        ] {
            assert!(own.relays.contains(h), "{h}");
        }
        assert_eq!(
            own.blossom,
            HashSet::from(["blossom.belfry.test".to_string()])
        );
        // without a domain: only what is listed, and the rules that need no name
        let bare = Own::from_config("", "", "", "");
        assert!(bare.api.is_empty() && bare.relays.is_empty() && bare.blossom.is_empty());
        let nip98 = serde_json::json!({"kind": 27235, "tags": [["u", "https://belfry.test/"]]});
        assert!(!forbidden(&nip98, &bare));
        assert!(forbidden(&nip98, &own));
        assert!(forbidden(&serde_json::json!({"kind": 9000}), &bare));
    }

    #[test]
    fn odd_templates_are_refused_not_guessed() {
        let own = vector_own();
        for event in [
            serde_json::json!("text"),
            serde_json::json!([1]),
            serde_json::json!(null),
            serde_json::json!({"kind": 1.0}),
            serde_json::json!({"kind": null}),
            serde_json::json!({"kind": 27235, "tags": 5}),
            serde_json::json!({"kind": 27235, "tags": "u"}),
            serde_json::json!({"kind": 27235, "tags": [["u", ["https://example.org"]]]}),
            serde_json::json!({"kind": 22242, "tags": [["relay", 5], ["relay", "wss://relay.belfry.test"]]}),
        ] {
            assert!(forbidden(&event, &own), "{event}");
        }
        // tags that are not lists, or of other names, are passed over
        let event = serde_json::json!({"kind": 27235,
            "tags": ["u", {"u": 1}, ["U", "https://belfry.test"], ["u", "https://example.org/"]]});
        assert!(!forbidden(&event, &own));
        assert!(!forbidden(&serde_json::json!({"kind": 1, "tags": 5}), &own));
    }
}
