// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// @title TrustLedger
/// @notice Append-only consortium ledger and Beta reputation contract for trust-aware synchromodal replanning.
///         Stakeholders sign their own reports; the LSP posts the interpreted claim of each report; sensor oracles
///         post independent records; the LSP commits plan and explanation hashes; the port authority posts the
///         realised events of a day. Any member can then score the day, which updates each sender's reputation
///         exactly as the reference implementation (stp/trust.py) does. Nothing can be edited or deleted.
/// @dev    Fixed-point numbers use 1e6 = 1.0. Claim kinds: 1 release, 2 lcs_delay, 3 volume (0 = not understood).
contract TrustLedger {
    // ------------------------------------------------------------------ roles and parameters
    uint8 public constant STAKEHOLDER = 1;
    uint8 public constant ORACLE = 2;
    uint8 public constant LSP = 3;
    uint8 public constant PORT_AUTHORITY = 4;

    int256 public constant ONE = 1e6;
    int256 public constant SHRINK = 2e6;     // pseudo-observations pulling per-kind trust towards pooled trust
    uint256 public constant WINDOW = 15;     // recent true/claimed ratios used for de-biasing
    int256 public constant TOL = 250000;     // a real claim has the right size if |true - claimed| <= 0.25

    address public immutable admin;
    int256 public immutable a0;              // Beta prior (a0, b0), fixed point
    int256 public immutable b0;

    mapping(address => uint8) public role;
    mapping(bytes32 => address) public ownerOf;   // asset (shipment or service) -> responsible stakeholder

    // ------------------------------------------------------------------ records
    struct Report {
        address sender;
        uint32 day;
        bytes32 textHash;
        bool claimPosted;
        uint8 kind;
        bytes32 target;
        int64 magnitude;
    }

    struct SensorRecord {
        address oracle;
        uint32 day;
        uint8 kind;
        bytes32 target;
        int64 magnitude;
    }

    struct Rep {
        uint32 real;
        uint32 falseClaims;
        uint32 omissions;
        uint32[4] realK;
        uint32[4] falseK;
        uint32[4] accurateK;
        uint32[4] ratioCount;
        int64[15][4] ratios;      // ring buffer of the last WINDOW true/claimed ratios per kind
    }

    Report[] public reports;
    SensorRecord[] public sensorRecords;
    mapping(uint32 => uint256[]) private dayReports;
    mapping(address => Rep) private rep;

    mapping(uint32 => bytes32) public planHash;
    mapping(uint32 => bytes32) public explanationHash;

    mapping(uint32 => bool) public outcomeStarted;
    mapping(uint32 => bool) public outcomePosted;
    mapping(uint32 => bool) public dayScored;
    mapping(uint32 => uint256) public scoreCursor;
    mapping(uint32 => bytes32[]) private truthKeys;
    mapping(uint32 => bytes32[]) private truthTargets;
    mapping(uint32 => mapping(bytes32 => bool)) private truthHas;
    mapping(uint32 => mapping(bytes32 => int64)) private truthMag;
    mapping(uint32 => mapping(bytes32 => bool)) private claimedKey;

    // ------------------------------------------------------------------ events
    event MemberRegistered(address indexed member, uint8 role);
    event ReportSubmitted(uint256 indexed id, address indexed sender, uint32 indexed day, bytes32 textHash);
    event ClaimPosted(uint256 indexed id, uint8 kind, bytes32 target, int64 magnitude);
    event SensorRecorded(uint256 indexed id, address indexed oracle, uint32 indexed day, uint8 kind, bytes32 target,
        int64 magnitude);
    event PlanCommitted(uint32 indexed day, bytes32 planHash, bytes32 explanationHash);
    event OutcomePosted(uint32 indexed day, uint256 nEvents);
    event DayScored(uint32 indexed day, uint256 nReports);

    error NotAuthorised();
    error AlreadyPosted();
    error BadInput();

    modifier only(uint8 r) {
        if (role[msg.sender] != r) revert NotAuthorised();
        _;
    }

    constructor(int256 priorA, int256 priorB) {
        if (priorA <= 0 || priorB <= 0) revert BadInput();
        admin = msg.sender;
        a0 = priorA;
        b0 = priorB;
    }

    // ------------------------------------------------------------------ consortium administration
    function registerMembers(address[] calldata members, uint8[] calldata roles) external {
        if (msg.sender != admin) revert NotAuthorised();
        if (members.length != roles.length) revert BadInput();
        for (uint256 i = 0; i < members.length; i++) {
            if (roles[i] < STAKEHOLDER || roles[i] > PORT_AUTHORITY) revert BadInput();
            role[members[i]] = roles[i];
            emit MemberRegistered(members[i], roles[i]);
        }
    }

    function setOwners(bytes32[] calldata assets, address[] calldata owners) external {
        if (msg.sender != admin) revert NotAuthorised();
        if (assets.length != owners.length) revert BadInput();
        for (uint256 i = 0; i < assets.length; i++) ownerOf[assets[i]] = owners[i];
    }

    // ------------------------------------------------------------------ daily writes
    /// @notice A stakeholder files a report; only the hash of the free text is stored on-chain.
    function submitReport(uint32 day, bytes32 textHash) external only(STAKEHOLDER) returns (uint256 id) {
        if (outcomeStarted[day]) revert AlreadyPosted();
        id = reports.length;
        reports.push(Report(msg.sender, day, textHash, false, 0, bytes32(0), 0));
        dayReports[day].push(id);
        emit ReportSubmitted(id, msg.sender, day, textHash);
    }

    /// @notice The LSP posts the interpretation of reports (kind 0 = not understood). Each report is interpreted once.
    function postClaims(uint256[] calldata ids, uint8[] calldata kinds, bytes32[] calldata targets,
        int64[] calldata magnitudes) external only(LSP) {
        if (ids.length != kinds.length || ids.length != targets.length || ids.length != magnitudes.length) {
            revert BadInput();
        }
        for (uint256 i = 0; i < ids.length; i++) {
            Report storage r = reports[ids[i]];
            if (r.claimPosted) revert AlreadyPosted();
            if (kinds[i] > 3) revert BadInput();
            r.claimPosted = true;
            r.kind = kinds[i];
            r.target = targets[i];
            r.magnitude = magnitudes[i];
            emit ClaimPosted(ids[i], kinds[i], targets[i], magnitudes[i]);
        }
    }

    function postSensorRecords(uint32 day, uint8[] calldata kinds, bytes32[] calldata targets,
        int64[] calldata magnitudes) external only(ORACLE) {
        if (kinds.length != targets.length || kinds.length != magnitudes.length) revert BadInput();
        for (uint256 i = 0; i < kinds.length; i++) {
            sensorRecords.push(SensorRecord(msg.sender, day, kinds[i], targets[i], magnitudes[i]));
            emit SensorRecorded(sensorRecords.length - 1, msg.sender, day, kinds[i], targets[i], magnitudes[i]);
        }
    }

    function commitPlan(uint32 day, bytes32 plan, bytes32 explanation) external only(LSP) {
        if (planHash[day] != bytes32(0)) revert AlreadyPosted();
        planHash[day] = plan;
        explanationHash[day] = explanation;
        emit PlanCommitted(day, plan, explanation);
    }

    /// @notice The port authority posts the realised events of a day, in one or more batches; the batch with
    ///         last = true closes the outcome. Once the first batch is posted, no report for that day is accepted.
    ///         Later duplicates of a (kind, target) overwrite the magnitude, as in the reference implementation.
    function postOutcome(uint32 day, uint8[] calldata kinds, bytes32[] calldata targets, int64[] calldata magnitudes,
        bool last) external only(PORT_AUTHORITY) {
        if (outcomePosted[day]) revert AlreadyPosted();
        if (kinds.length != targets.length || kinds.length != magnitudes.length) revert BadInput();
        outcomeStarted[day] = true;
        if (last) outcomePosted[day] = true;
        for (uint256 i = 0; i < kinds.length; i++) {
            bytes32 key = keccak256(abi.encode(kinds[i], targets[i]));
            if (!truthHas[day][key]) {
                truthHas[day][key] = true;
                truthKeys[day].push(key);
                truthTargets[day].push(targets[i]);
            }
            truthMag[day][key] = magnitudes[i];
        }
        if (last) emit OutcomePosted(day, truthKeys[day].length);
    }

    // ------------------------------------------------------------------ scoring
    /// @notice Score up to maxReports unscored reports of a day; when all are scored, count omissions and close
    ///         the day. Callable by anyone, so scoring cannot be withheld. Returns true when the day is closed.
    function scoreDay(uint32 day, uint256 maxReports) external returns (bool closed) {
        if (!outcomePosted[day] || dayScored[day]) revert BadInput();
        uint256[] storage ids = dayReports[day];
        uint256 c = scoreCursor[day];
        uint256 end = c + maxReports;
        if (end > ids.length) end = ids.length;
        for (; c < end; c++) {
            Report storage r = reports[ids[c]];
            if (!r.claimPosted || r.kind == 0) continue;           // not understood: not scored
            bytes32 key = keccak256(abi.encode(r.kind, r.target));
            claimedKey[day][key] = true;
            Rep storage s = rep[r.sender];
            if (truthHas[day][key]) {
                s.real += 1;
                s.realK[r.kind] += 1;
                if (r.magnitude != 0) {
                    int64 tm = truthMag[day][key];
                    uint32 n = s.ratioCount[r.kind];
                    s.ratios[r.kind][n % WINDOW] = int64((int256(tm) * ONE) / int256(r.magnitude));
                    s.ratioCount[r.kind] = n + 1;
                    int256 diff = int256(tm) - int256(r.magnitude);
                    if (diff < 0) diff = -diff;
                    if (diff <= TOL) s.accurateK[r.kind] += 1;
                }
            } else {
                s.falseClaims += 1;
                s.falseK[r.kind] += 1;
            }
        }
        scoreCursor[day] = c;
        if (c == ids.length) {
            bytes32[] storage keys = truthKeys[day];
            for (uint256 i = 0; i < keys.length; i++) {
                if (!claimedKey[day][keys[i]]) {
                    address o = ownerOf[truthTargets[day][i]];
                    if (o != address(0)) rep[o].omissions += 1;
                }
            }
            dayScored[day] = true;
            emit DayScored(day, ids.length);
            return true;
        }
        return false;
    }

    // ------------------------------------------------------------------ views (what the planner reads)
    function pReal(address who) public view returns (int256) {
        Rep storage s = rep[who];
        return ((a0 + int256(uint256(s.real)) * ONE) * ONE)
            / (a0 + b0 + int256(uint256(s.real) + uint256(s.falseClaims)) * ONE);
    }

    function pKind(address who, uint8 kind) public view returns (int256) {
        Rep storage s = rep[who];
        int256 nk = int256(uint256(s.realK[kind]) + uint256(s.falseK[kind]));
        return ((SHRINK * pReal(who)) / ONE + int256(uint256(s.realK[kind])) * ONE) * ONE / (SHRINK + nk * ONE);
    }

    function qKind(address who, uint8 kind) public view returns (int256) {
        Rep storage s = rep[who];
        return (int256(1 + uint256(s.accurateK[kind])) * ONE) / int256(2 + uint256(s.realK[kind]));
    }

    /// @notice Median of the last WINDOW true/claimed ratios (1.0 until two are available).
    function factor(address who, uint8 kind) public view returns (int256) {
        Rep storage s = rep[who];
        uint256 n = s.ratioCount[kind];
        if (n > WINDOW) n = WINDOW;
        if (n < 2) return ONE;
        int256[] memory v = new int256[](n);
        for (uint256 i = 0; i < n; i++) v[i] = s.ratios[kind][i];
        for (uint256 i = 1; i < n; i++) {               // insertion sort, n <= 15
            int256 x = v[i];
            uint256 j = i;
            while (j > 0 && v[j - 1] > x) {
                v[j] = v[j - 1];
                j--;
            }
            v[j] = x;
        }
        return n % 2 == 1 ? v[n / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
    }

    /// @notice Full reputation state of one stakeholder; arrays are indexed by kind 1..3 (index 0 unused).
    function reputation(address who) external view returns (uint32 real, uint32 falseClaims, uint32 omissions,
        int256 p, int256[4] memory pk, int256[4] memory qk, int256[4] memory fk, uint32[4] memory accurate) {
        Rep storage s = rep[who];
        real = s.real;
        falseClaims = s.falseClaims;
        omissions = s.omissions;
        p = pReal(who);
        for (uint8 k = 1; k <= 3; k++) {
            pk[k] = pKind(who, k);
            qk[k] = qKind(who, k);
            fk[k] = factor(who, k);
        }
        accurate = s.accurateK;
    }

    function reportCount() external view returns (uint256) {
        return reports.length;
    }

    function dayReportIds(uint32 day) external view returns (uint256[] memory) {
        return dayReports[day];
    }
}
